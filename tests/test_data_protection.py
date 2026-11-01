"""Защита хранимых данных (аудит 2026-10-30, этап 1): секреты из файлов, стартовый пароль администратора,
журнал входов без введённых строк, затирание удалённого, WAL checkpoint, логи без query, заголовки."""
import logging
import re

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from app import config as config_module
from app.config import config
from app.db import checkpoint_wal, engine, sqlite_path
from app.main import StripQueryFromAccessLog, app
from app.models import LoginAttempt, User
from app.services.permissions import ensure_default_admin
from app.services.security import verify_password


def _csrf(c: TestClient) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text).group(1)


def test_secret_from_file_wins_over_env(tmp_path, monkeypatch):
    f = tmp_path / "secret_key"
    f.write_text("from-file\n")
    monkeypatch.setenv("SECRET_KEY", "from-env")
    monkeypatch.setenv("SECRET_KEY_FILE", str(f))
    assert config_module._secret("SECRET_KEY") == "from-file"
    monkeypatch.delenv("SECRET_KEY_FILE")
    assert config_module._secret("SECRET_KEY") == "from-env"


def test_first_admin_password_from_config(db, monkeypatch):
    db.execute(delete(User))
    db.commit()
    monkeypatch.setattr(config, "first_admin_password", "long-start-password")
    assert ensure_default_admin(db)
    admin = db.scalar(select(User))
    assert admin.username == "admin" and admin.must_change_credentials
    assert verify_password(admin.password_hash, "long-start-password")
    assert not verify_password(admin.password_hash, "admin")


def test_unknown_login_not_stored_but_lockout_works(client, db):
    for _ in range(5):
        r = client.post("/login", data={"username": "MyRealPassw0rd!", "password": "x", "csrf_token": _csrf(client)})
        assert r.status_code == 401
    r = client.post("/login", data={"username": "другая-опечатка", "password": "x", "csrf_token": _csrf(client)})
    assert r.status_code == 429  # неизвестные логины с одного IP считаются вместе
    names = set(db.scalars(select(LoginAttempt.username)))
    assert names == {""}
    # Существующий пользователь с того же IP не заблокирован чужими опечатками
    r = client.post("/login", data={"username": "user", "password": "test-password-123",
                                    "csrf_token": _csrf(client)}, follow_redirects=False)
    assert r.status_code == 303


def test_secure_delete_on():
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA secure_delete")).scalar() == 1


def test_checkpoint_truncates_wal(db):
    db.add(User(username="wal-filler", password_hash="x" * 200))
    db.commit()
    wal = sqlite_path(config.database_url).with_name(sqlite_path(config.database_url).name + "-wal")
    checkpoint_wal()
    assert not wal.exists() or wal.stat().st_size == 0


def test_access_log_without_query():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/confluence/search?q=пароль роутера", "1.1", 200), None)
    assert StripQueryFromAccessLog().filter(rec)
    assert rec.getMessage() == '1.2.3.4:5 - "GET /confluence/search HTTP/1.1" 200'


def test_permissions_policy_and_hsts(client, monkeypatch):
    r = client.get("/login")
    assert "camera=()" in r.headers["Permissions-Policy"]
    assert "Strict-Transport-Security" not in r.headers  # по HTTP не выставляем
    monkeypatch.setattr(config, "cookie_secure", True)
    assert client.get("/login").headers["Strict-Transport-Security"].startswith("max-age=")
