"""Бэкап по расписанию и проверка восстановления. Всегда во временном каталоге, не в боевом."""
import secrets
import shutil
from datetime import UTC, datetime, timedelta

import pyrage
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import migrate
from app.config import config
from app.db import connect_raw, make_engine, sqlite_path
from app.models import BackupLog, Notification, SecurityEvent, User
from app.services import backup
from app.services.settings_registry import set_setting
from app.utils import utcnow


@pytest.fixture
def bk(tmp_path, monkeypatch):
    path = tmp_path / "bk"
    monkeypatch.setattr(config, "backup_dir", str(path))
    monkeypatch.setattr(config, "backup_age_recipient", "")
    return path


def _files(path):
    return sorted(p.name for p in path.iterdir())


def test_make_backup_with_verify(bk):
    res = backup.make_backup(sqlite_path(config.database_url), bk, 14, verify=True)
    assert res.ok and res.path.exists() and res.size > 0
    assert res.verify.ok, res.verify.detail
    assert _files(bk) == [res.path.name]  # ни .tmp, ни временных каталогов проверки


def test_run_backup_logs_and_age_verified_before_encryption(bk, monkeypatch, db):
    ident = pyrage.x25519.Identity.generate()
    monkeypatch.setattr(config, "backup_age_recipient", str(ident.to_public()))
    res = backup.run_backup("manual", verify=True)
    assert res.ok and res.path.name.endswith(".db.age") and res.verify.ok
    assert _files(bk) == [res.path.name]
    row = db.scalar(select(BackupLog))
    assert row.ok and row.verified and row.verify_ok and row.file == res.path.name and row.trigger == "manual"
    assert str(bk) not in row.file  # в журнале только имя файла
    # И расшифрованное владельцем содержимое — рабочая БД
    plain = bk / "dec.db"
    plain.write_bytes(pyrage.decrypt(res.path.read_bytes(), [ident]))
    assert connect_raw(plain).execute("SELECT count(*) FROM users").fetchone()[0] == 3


def test_rotation_keeps_n(bk):
    src = sqlite_path(config.database_url)
    bk.mkdir()
    for i in range(4):
        old = bk / f"homelab-2020010{i}-000000.db"
        old.write_bytes(b"x")
    res = backup.make_backup(src, bk, 3)
    assert res.ok and res.deleted == 2 and len(_files(bk)) == 3


def test_verify_detects_missing_rows(bk, tmp_path):
    src = sqlite_path(config.database_url)
    good = backup.make_backup(src, bk, 14).path
    bad = tmp_path / "bad.db"
    shutil.copyfile(good, bad)
    with connect_raw(bad) as c:
        c.execute("DELETE FROM users")
        c.commit()
        c.execute("PRAGMA journal_mode=DELETE")  # изменения — в основной файл, не в -wal
    res = backup.verify_copy(bad, src, tmp_path)
    assert not res.ok and "users" in res.detail
    assert [p.name for p in tmp_path.glob("verify-*")] == []  # временный каталог убран


def test_verify_garbage_file(bk, tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database" * 100)
    res = backup.verify_copy(junk, sqlite_path(config.database_url), tmp_path)
    assert not res.ok and "не открывается" in res.detail


def test_verify_encrypted_db(tmp_path, monkeypatch):
    path = tmp_path / "enc.db"
    monkeypatch.setattr(config, "database_url", f"sqlite:///{path}")
    monkeypatch.setattr(config, "db_encryption_key", secrets.token_hex(32))
    monkeypatch.setattr(config, "backup_dir", str(tmp_path / "bk"))
    migrate.upgrade()
    eng = make_engine(config.database_url)
    with Session(eng) as s:
        s.add(User(username="enc-user", password_hash="x"))
        s.commit()
    eng.dispose()
    res = backup.make_backup(path, tmp_path / "bk", 14, verify=True)
    assert res.ok and res.verify.ok, res.verify.detail if res.verify else res.error


def test_failure_notifies_admins_and_audits(bk, monkeypatch, db):
    def boom(*a, **k):
        raise OSError("диск полон")

    monkeypatch.setattr(backup, "make_backup", boom)
    res = backup.run_backup("auto")
    assert not res.ok and "диск полон" in res.error
    db.expire_all()
    admin = db.scalar(select(User).where(User.username == "admin"))
    viewer = db.scalar(select(User).where(User.username == "viewer"))
    assert db.scalar(select(Notification).where(Notification.user_id == admin.id, Notification.title.like("Бэкап не удался%")))
    assert not db.scalar(select(Notification).where(Notification.user_id == viewer.id))
    ev = db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "backup"))
    assert ev and not ev.ok
    assert not db.scalar(select(BackupLog)).ok


def test_verify_failure_is_reported(bk, monkeypatch, db):
    monkeypatch.setattr(backup, "verify_copy", lambda *a, **k: backup.VerifyResult(False, "строки потерялись"))
    res = backup.run_verify()
    assert not res.ok
    db.expire_all()
    assert db.scalar(select(Notification).where(Notification.title.like("Проверка восстановления%")))
    assert db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "backup_verify"))
    assert _files(bk) == []  # снимок не остался в каталоге бэкапов


def test_schedule_due(bk, db):
    day = datetime(2026, 11, 5, 3, 0)
    assert not backup.due(db, day.replace(hour=5))  # выключено
    set_setting(db, "backups.enabled", True)
    set_setting(db, "backups.hour", 4)
    assert not backup.due(db, day)  # ещё не 4 часа
    assert backup.due(db, day.replace(hour=4, minute=10))
    assert backup.next_run(db, day).hour == 4
    # Удачный бэкап «сегодня» (после слота) → больше не нужен
    slot_utc = day.replace(hour=4).astimezone(UTC).replace(tzinfo=None)
    db.add(BackupLog(ts=slot_utc + timedelta(minutes=5), ok=True, file="a.db"))
    db.commit()
    assert not backup.due(db, day.replace(hour=9))
    assert backup.next_run(db, day.replace(hour=9)).day == 6


def test_schedule_off_without_backup_dir(db):
    set_setting(db, "backups.enabled", True)
    assert not config.backup_dir and not backup.due(db, datetime(2026, 11, 5, 12))


def test_verify_due_weekly(db):
    assert backup.verify_due(db)
    db.add(BackupLog(ok=True, verified=True, verify_ok=True))
    db.commit()
    assert not backup.verify_due(db)
    assert backup.verify_due(db, utcnow() + timedelta(days=8))


def test_page_not_configured(admin):
    r = admin.get("/settings/backups")
    assert r.status_code == 200 and "BACKUP_DIR" in r.text
    assert 'href="/settings/backups"' in r.text  # пункт в меню настроек


def test_page_permissions(user, viewer, client):
    assert user.get("/settings/backups").status_code == 403
    assert viewer.get("/settings/backups").status_code == 403
    assert user.post("/settings/backups/run").status_code == 403
    assert client.get("/settings/backups", follow_redirects=False).status_code in (303, 401)


def test_page_actions(admin, bk, db):
    assert admin.post("/settings/backups/run", follow_redirects=False).status_code == 303
    r = admin.get("/settings/backups")
    assert str(bk) in r.text and "Проверить восстановление" in r.text
    row = db.scalar(select(BackupLog).where(BackupLog.kind == "backup"))
    assert row.ok and row.verify_ok and row.trigger == "manual"
    assert admin.post("/settings/backups/verify", follow_redirects=False).status_code == 303
    assert len(db.scalars(select(BackupLog)).all()) == 2


def test_page_save_settings_and_csrf(admin, bk, db):
    r = admin.post("/settings/backups", data={"enabled": "on", "hour": "7", "keep": "5"}, follow_redirects=False)
    assert r.status_code == 303
    assert backup.enabled(db) and backup.next_run(db).hour == 7
    assert admin.post("/settings/backups", data={"hour": "99", "keep": "5"}).status_code == 400
    admin.headers.pop("X-CSRF-Token")
    assert admin.post("/settings/backups/run").status_code == 403


def test_cli_backup_uses_service(bk, capsys):
    from app import cli

    assert cli.main(["backup", "--dir", str(bk), "--no-age"]) == 0
    assert "Бэкап:" in capsys.readouterr().out
    assert len(_files(bk)) == 1
