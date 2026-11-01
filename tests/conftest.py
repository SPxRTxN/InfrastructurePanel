"""Тесты работают на временной SQLite-БД, схема создаётся миграциями Alembic.

Переменные окружения выставляются до импорта app, т.к. config читается при импорте.
"""
import os
import re
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

_TMP = Path(tempfile.mkdtemp(prefix="homelab-test-"))
# Боевой .env не читается, а всё, что могло прийти из окружения (ключи, каталоги бэкапов), — обнуляется
os.environ["HOMELAB_IGNORE_DOTENV"] = "1"
for _name in ("SECRET_KEY", "DB_ENCRYPTION_KEY", "PASSWORD_PEPPER", "FIRST_ADMIN_PASSWORD"):
    os.environ.pop(f"{_name}_FILE", None)
for _name in ("DB_ENCRYPTION_KEY", "PASSWORD_PEPPER", "FIRST_ADMIN_PASSWORD", "BACKUP_DIR", "BACKUP_AGE_RECIPIENT"):
    os.environ.pop(_name, None)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["SECRET_KEY"] = Fernet.generate_key().decode()
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["COOKIE_SECURE"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import migrate  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Group, User  # noqa: E402
from app.services.permissions import ensure_default_groups  # noqa: E402
from app.services.security import hash_password  # noqa: E402

PASSWORD = "test-password-123"  # только для временной тестовой БД
USERS = {"admin": "Администраторы", "user": "Пользователи", "viewer": "Наблюдатели"}


@pytest.fixture(scope="session", autouse=True)
def _schema():
    # Защита от повторения 2026-10-31: тесты не должны трогать настоящие ключи, БД и бэкапы
    from app.config import config

    assert not (config.db_encryption_key or config.password_pepper or config.backup_dir
                or config.backup_age_recipient), "тесты видят боевые настройки"
    assert str(_TMP) in config.database_url
    migrate.upgrade()
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def db():
    """Чистая БД перед каждым тестом: удаляем строки всех таблиц, создаём группы и тест-пользователей."""
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    session = SessionLocal()
    ensure_default_groups(session)
    pw_hash = hash_password(PASSWORD)
    for username, group in USERS.items():
        g = session.scalar(select(Group).where(Group.name == group))
        session.add(User(username=username, password_hash=pw_hash, groups=[g]))
    session.commit()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _no_github(monkeypatch):
    """Страница обновления спрашивает релизы на GitHub — в тестах сети нет: пустой список и чистый кэш."""
    from app.services import releases

    monkeypatch.setattr(releases, "fetch", lambda repo: [])
    releases._cache.update(at=0.0, repo="", data=None)


def login(client: TestClient, username: str, password: str = PASSWORD) -> TestClient:
    r = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    r = client.post("/login", data={"username": username, "password": password, "csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    page = client.get("/")
    client.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    return client


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin():
    return login(TestClient(app), "admin")


@pytest.fixture
def user():
    return login(TestClient(app), "user")


@pytest.fixture
def viewer():
    return login(TestClient(app), "viewer")
