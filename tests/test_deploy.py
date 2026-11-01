"""Готовность к Docker и переносу: WAL, права файлов БД, бэкап, проверка ключа, диски в контейнере,
содержимое Dockerfile/compose/.dockerignore и актуальность requirements.lock."""
import os
import sqlite3
import stat
from collections import namedtuple
from pathlib import Path

import pytest
from sqlalchemy import text

from app import cli
from app.config import BASE_DIR, config
from app.db import engine, secure_db_files, sqlite_path
from app.services import monitoring

Part = namedtuple("Part", "device mountpoint fstype opts")


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_sqlite_wal_and_busy_timeout():
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000


def test_secure_db_files(tmp_path):
    db = tmp_path / "data" / "x.db"
    db.parent.mkdir(mode=0o775)
    sqlite3.connect(db).close()
    os.chmod(db, 0o644)
    secure_db_files(f"sqlite:///{db}")
    assert _mode(db) == 0o600 and _mode(db.parent) == 0o700


def test_backup_rotation_and_permissions(tmp_path, capsys):
    out = tmp_path / "backups"
    out.mkdir()
    for name in ("homelab-20000101-000000.db", "homelab-20000102-000000.db"):
        (out / name).write_bytes(b"old")
    assert cli.main(["backup", "--dir", str(out), "--keep", "2"]) == 0
    files = sorted(out.glob("homelab-*.db"))
    assert [f.name for f in files][0] == "homelab-20000102-000000.db" and len(files) == 2
    new = files[-1]
    assert _mode(new) == 0o600
    with sqlite3.connect(new) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # самодостаточный файл
        assert c.execute("SELECT count(*) FROM users WHERE username='admin'").fetchone()[0] == 1
    assert "SECRET_KEY" in capsys.readouterr().out
    assert not list(out.glob("*.tmp"))


def test_backup_default_dir_next_to_db():
    db = sqlite_path(config.database_url)
    assert cli.main(["backup", "--keep", "1"]) == 0
    assert len(list((db.parent / "backups").glob("homelab-*.db"))) == 1


@pytest.mark.parametrize("key", ["", "not-a-fernet-key"])
def test_serve_refuses_without_valid_secret_key(monkeypatch, capsys, key):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(cli.config, "secret_key", key)
    assert cli.main(["serve"]) == 1
    assert not calls and "SECRET_KEY" in capsys.readouterr().out


def test_collect_local_skips_file_mounts(monkeypatch, tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost")
    parts = [Part("/dev/sda1", str(hosts), "ext4", "ro"), Part("/dev/sda1", str(tmp_path), "ext4", "rw")]
    monkeypatch.setattr(monitoring.psutil, "disk_partitions", lambda all=False: parts)
    assert [d["mount"] for d in monitoring.collect_local().disks] == [str(tmp_path)]


def _read(name: str) -> str:
    return (BASE_DIR / name).read_text()


def test_dockerignore_is_allowlist():
    lines = [x.strip() for x in _read(".dockerignore").splitlines() if x.strip() and not x.startswith("#")]
    assert lines[0] == "*"
    allowed = {x[1:].rstrip("/") for x in lines if x.startswith("!")}
    # handoff/homelab-agent — агент, встроенный в образ для обновления узлов из панели (без тестов, конфига, токенов)
    assert allowed == {"app", "migrations", "alembic.ini", "requirements.txt", "requirements.lock",
                       "handoff/homelab-agent"}
    for excluded in ("handoff/homelab-agent/tests/", "handoff/homelab-agent/agent.toml", "handoff/homelab-agent/**/token-*"):
        assert excluded in lines


def test_dockerfile_hardening():
    df = _read("Dockerfile")
    assert "USER app" in df and "HEALTHCHECK" in df and "/healthz" in df
    assert "-c requirements.lock" in df
    assert "DATABASE_URL=sqlite:////data/homelab.db" in df
    assert '"app.cli", "serve"' in df  # один процесс, без --workers
    copies = [x for x in df.splitlines() if x.startswith("COPY")]
    assert all(".env" not in x and "requirements-dev" not in x and "COPY . " not in x for x in copies)


def test_compose_hardening():
    c = _read("compose.yaml")
    for needle in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true", "env_file: .env",
                   "${DATA_DIR:-./data}:/data", "${BACKUP_DIR:-./data/backups}:/data/backups",
                   "BACKUP_DIR: /data/backups", "/etc/localtime:/etc/localtime:ro", "restart: unless-stopped",
                   "${DASHBOARD_BIND:-127.0.0.1}"):
        assert needle in c, needle
    assert "SECRET_KEY" not in c


def test_requirements_lock_is_current():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lock_deps", BASE_DIR / "tools" / "lock_deps.py")
    lock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lock)
    assert lock.LOCK.read_text() == lock.render(lock.resolve()), "запустите tools/lock_deps.py"
    assert "pytest" not in lock.LOCK.read_text()  # тестовые пакеты не попадают в прод-образ


def test_predeploy_backup_found_after_backup_dir_change():
    """Регрессия 2026-11-01: сменили BACKUP_DIR в .env — бэкап перед выкладкой пишет ещё старый контейнер в свой
    каталог; deploy.sh должен искать его там (по монтированию контейнера), а без бэкапа — остановиться."""
    sh = _read("tools/deploy.sh")
    assert '{{if eq .Destination "/data/backups"}}{{.Source}}' in sh
    assert 'ls -1t "$OLD_BACKUP_DIR"/homelab-*.db' in sh
    assert "выкладка остановлена, ничего не изменено" in sh
    # Проверка бэкапа — до сборки и запуска нового образа
    assert sh.index("OLD_BACKUP_DIR") < sh.index("docker build")
