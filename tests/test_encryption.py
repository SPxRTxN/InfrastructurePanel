"""Защита данных, этап 2: SQLCipher, конвертация открытой БД, шифрованные бэкапы (age), «перец» паролей."""
import secrets
import sqlite3

import pyrage
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cli, migrate
from app.config import config
from app.db import check_db_encryption, connect_raw, is_encrypted_file, make_engine
from app.models import Page, Space, User
from app.services import security
from tests.conftest import PASSWORD, login

MARKER = "SECRET-MARKER-wifi-Hunter2"


def _all_bytes(path) -> bytes:
    return b"".join(p.read_bytes() for p in path.parent.glob(path.name + "*"))


@pytest.fixture
def enc_db(tmp_path, monkeypatch):
    """Отдельная зашифрованная БД со схемой и статьёй-маркером; config смотрит на неё."""
    key = secrets.token_hex(32)
    path = tmp_path / "enc.db"
    url = f"sqlite:///{path}"
    monkeypatch.setattr(config, "database_url", url)
    monkeypatch.setattr(config, "db_encryption_key", key)
    migrate.upgrade(url)
    eng = make_engine(url)
    with Session(eng) as s:
        space = Space(key="SEC", name="Секреты")
        s.add(Page(space=space, title="Роутер", body=MARKER))
        s.commit()
    with eng.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    eng.dispose()
    return path, key


def test_encrypted_db_unreadable_without_key(enc_db):
    path, key = enc_db
    assert is_encrypted_file(path)
    assert MARKER.encode() not in _all_bytes(path)
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(path).execute("SELECT * FROM pages").fetchall()
    import sqlcipher3

    with pytest.raises(sqlcipher3.DatabaseError):
        connect_raw(path, key=secrets.token_hex(32)).execute("SELECT * FROM pages").fetchall()
    assert connect_raw(path, key=key).execute("SELECT body FROM pages").fetchone()[0] == MARKER


def test_mode_mismatch_gives_clear_error(enc_db, tmp_path):
    path, _ = enc_db
    with pytest.raises(RuntimeError, match="зашифрована"):
        check_db_encryption(f"sqlite:///{path}", key="")
    plain = tmp_path / "plain.db"
    sqlite3.connect(plain).execute("CREATE TABLE t(x)").connection.commit()
    with pytest.raises(RuntimeError, match="encrypt-db"):
        check_db_encryption(f"sqlite:///{plain}", key=secrets.token_hex(32))
    with pytest.raises(ValueError, match="64 hex"):
        make_engine(f"sqlite:///{tmp_path / 'x.db'}", key="short").connect()


def test_encrypt_existing_plain_db(tmp_path, monkeypatch, capsys):
    path = tmp_path / "homelab.db"
    url = f"sqlite:///{path}"
    migrate.upgrade(url)  # ключ в config пуст (conftest) — открытая БД, как у существующих установок
    eng = make_engine(url, key="")
    with Session(eng) as s:
        s.add(Page(space=Space(key="SEC", name="S"), title="t", body=MARKER))
        s.commit()
    eng.dispose()
    (tmp_path / "backups").mkdir()
    sqlite3.connect(tmp_path / "backups" / "homelab-old.db").execute("CREATE TABLE t(x)").connection.commit()

    key = secrets.token_hex(32)
    monkeypatch.setattr(config, "database_url", url)
    monkeypatch.setattr(config, "db_encryption_key", key)
    assert cli.main(["encrypt-db"]) == 0
    out = capsys.readouterr().out
    assert "зашифрована" in out and "1 старых бэкапов" in out
    assert is_encrypted_file(path) and MARKER.encode() not in _all_bytes(path)
    assert not list(tmp_path.glob("*.enc-tmp"))
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert connect_raw(path).execute("SELECT body FROM pages").fetchone()[0] == MARKER
    assert cli.main(["encrypt-db"]) == 0 and "уже зашифрована" in capsys.readouterr().out
    # После шифрования приложение работает с БД штатно: миграции, бэкап
    assert cli.main(["backup", "--no-age", "--keep", "1"]) == 0


def test_backup_of_encrypted_db_is_encrypted(enc_db):
    path, key = enc_db
    assert cli.main(["backup", "--dir", str(path.parent / "bk"), "--no-age"]) == 0
    copy = next((path.parent / "bk").glob("homelab-*.db"))
    assert is_encrypted_file(copy) and MARKER.encode() not in copy.read_bytes()
    assert connect_raw(copy, key=key).execute("SELECT body FROM pages").fetchone()[0] == MARKER


def test_backup_age_only_private_key_decrypts(enc_db, monkeypatch):
    path, key = enc_db
    ident = pyrage.x25519.Identity.generate()
    monkeypatch.setattr(config, "backup_age_recipient", str(ident.to_public()))
    out = path.parent / "bk"
    assert cli.main(["backup", "--dir", str(out)]) == 0
    files = list(out.iterdir())
    assert [f.suffix for f in files] == [".age"] and oct(files[0].stat().st_mode & 0o777) == "0o600"
    with pytest.raises(pyrage.DecryptError):
        pyrage.decrypt(files[0].read_bytes(), [pyrage.x25519.Identity.generate()])
    restored = path.parent / "restored.db"
    restored.write_bytes(pyrage.decrypt(files[0].read_bytes(), [ident]))
    assert connect_raw(restored, key=key).execute("SELECT body FROM pages").fetchone()[0] == MARKER


def test_backup_age_plain_db(tmp_path, monkeypatch):
    """age работает и без SQLCipher: открытая копия никогда не ложится на диск."""
    ident = pyrage.x25519.Identity.generate()
    out = tmp_path / "bk"
    assert cli.main(["backup", "--dir", str(out), "--age-recipient", str(ident.to_public())]) == 0
    data = pyrage.decrypt(next(out.glob("*.age")).read_bytes(), [ident])
    assert data.startswith(b"SQLite format 3\x00")
    assert not list(out.glob("*.db")) and not list(out.glob("*.tmp"))


def test_setup_secrets_creates_and_never_overwrites(tmp_path, capsys):
    import importlib.util

    from app.config import BASE_DIR

    spec = importlib.util.spec_from_file_location("setup_secrets", BASE_DIR / "tools" / "setup_secrets.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    target = tmp_path / "secrets"
    assert tool.main([str(target)]) == 0
    out = capsys.readouterr().out
    files = {p.name: p for p in target.iterdir()}
    assert set(files) == {"secret_key", "db_encryption_key", "password_pepper", "first_admin_password"}
    assert oct(target.stat().st_mode & 0o777) == "0o700"
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in files.values())
    assert files["secret_key"].read_text().strip() == config.secret_key  # перенесён, а не новый
    db_key = files["db_encryption_key"].read_text().strip()
    assert len(db_key) == 64 and db_key not in out and config.secret_key not in out  # ключи не печатаются
    assert tool.main([str(target)]) == 0
    assert files["db_encryption_key"].read_text().strip() == db_key  # повторный запуск не перезаписал
    assert "уже есть" in capsys.readouterr().out


def test_backup_dir_from_config(enc_db, monkeypatch, tmp_path):
    other_disk = tmp_path / "other-disk" / "homelab-backups"
    monkeypatch.setattr(config, "backup_dir", str(other_disk))
    assert cli.main(["backup", "--no-age"]) == 0
    assert len(list(other_disk.glob("homelab-*.db"))) == 1


def test_pepper_hash_and_verify(monkeypatch):
    monkeypatch.setattr(config, "password_pepper", "pepper-from-secret-file")
    h = security.hash_password("hunter2-long")
    assert h.startswith("p1$$argon2id$")
    assert security.verify_password(h, "hunter2-long") and not security.verify_password(h, "wrong")
    assert not security.needs_rehash(h)
    monkeypatch.setattr(config, "password_pepper", "другой перец")
    assert not security.verify_password(h, "hunter2-long")
    monkeypatch.setattr(config, "password_pepper", "")
    assert not security.verify_password(h, "hunter2-long")  # без перца хеш не проверить


def test_login_upgrades_old_hash_to_pepper(client, db, monkeypatch):
    old = db.scalar(select(User.password_hash).where(User.username == "user"))
    assert old.startswith("$argon2id$")
    monkeypatch.setattr(config, "password_pepper", "pepper-from-secret-file")
    login(client, "user")  # старый хеш без перца всё ещё принимается
    db.expire_all()
    new = db.scalar(select(User.password_hash).where(User.username == "user"))
    assert new.startswith(security.PEPPER_PREFIX) and security.verify_password(new, PASSWORD)
