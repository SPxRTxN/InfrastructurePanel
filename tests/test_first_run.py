"""Первое развёртывание: admin/admin с обязательной сменой логина и пароля.
Перезапуск/обновление: пользователи и данные не пересоздаются."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.main import app
from app.models import Board, Group, GroupPermission, User, UserSession
from app.services.permissions import ADMIN_GROUP, ensure_default_admin
from tests.conftest import PASSWORD

NEW_PASSWORD = "brand-new-password-1"


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _login(username: str, password: str) -> TestClient:
    c = TestClient(app)
    token = _csrf(c.get("/login").text)
    r = c.post("/login", data={"username": username, "password": password, "csrf_token": token},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


@pytest.fixture
def fresh(db):
    """БД как при первом развёртывании: группы есть, пользователей нет."""
    db.execute(delete(User))
    db.commit()
    assert ensure_default_admin(db) is True
    return db


def _setup(c: TestClient, **data):
    token = _csrf(c.get("/setup").text)
    return c.post("/setup", data={"csrf_token": token, **data}, follow_redirects=False)


def test_default_admin_only_on_empty_db(fresh):
    admin = fresh.scalar(select(User))
    assert admin.username == "admin" and admin.must_change_credentials
    assert admin.can("users.manage") and [g.name for g in admin.groups] == [ADMIN_GROUP]
    assert admin.password_hash.startswith("$argon2id$")
    assert ensure_default_admin(fresh) is False
    assert fresh.scalar(select(func.count()).select_from(User)) == 1


def test_no_default_admin_when_users_exist(db):
    assert ensure_default_admin(db) is False
    assert db.scalar(select(User).where(User.must_change_credentials.is_(True))) is None


def test_everything_closed_until_setup(fresh):
    c = _login("admin", "admin")
    for path in ("/", "/servers", "/jira", "/settings/users", "/settings/profile"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup", path
    assert c.get("/api/notifications").status_code == 403
    r = c.get("/servers", headers={"HX-Request": "true"})
    assert r.status_code == 403 and r.headers["HX-Redirect"] == "/setup"
    token = _csrf(c.get("/setup").text)
    r = c.post("/settings/users", data={"csrf_token": token, "username": "x", "password": "long-enough-1"},
               follow_redirects=False)
    assert r.headers["location"] == "/setup"
    assert fresh.scalar(select(User).where(User.username == "x")) is None


@pytest.mark.parametrize("data, message", [
    ({"username": "admin", "password": NEW_PASSWORD, "password2": NEW_PASSWORD}, "новый логин"),
    ({"username": "ADMIN", "password": NEW_PASSWORD, "password2": NEW_PASSWORD}, "новый логин"),
    ({"username": "owner", "password": "short", "password2": "short"}, "не короче"),
    ({"username": "owner", "password": NEW_PASSWORD, "password2": NEW_PASSWORD + "x"}, "не совпадают"),
    ({"username": "taken", "password": NEW_PASSWORD, "password2": NEW_PASSWORD}, "занят"),
])
def test_setup_validation(fresh, data, message):
    fresh.add(User(username="taken", password_hash="x"))
    fresh.commit()
    c = _login("admin", "admin")
    r = _setup(c, **data)
    assert r.status_code == 400 and message in r.text
    assert NEW_PASSWORD not in r.text
    fresh.expire_all()
    assert fresh.scalar(select(User).where(User.username == "admin")).must_change_credentials


def test_setup_success(fresh):
    other = _login("admin", "admin")  # кто-то ещё вошёл под admin/admin
    c = _login("admin", "admin")
    r = _setup(c, username="owner", password=NEW_PASSWORD, password2=NEW_PASSWORD)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/").status_code == 200
    assert c.get("/setup", follow_redirects=False).headers["location"] == "/"
    assert other.get("/", follow_redirects=False).headers["location"].startswith("/login")

    fresh.expire_all()
    user = fresh.scalar(select(User))
    assert user.username == "owner" and not user.must_change_credentials
    assert fresh.scalar(select(func.count()).select_from(UserSession)) == 1
    bad = TestClient(app)
    r = bad.post("/login", data={"username": "admin", "password": "admin", "csrf_token": _csrf(bad.get("/login").text)})
    assert r.status_code == 401
    _login("owner", NEW_PASSWORD)


def test_regular_users_never_see_setup(admin):
    assert admin.get("/setup", follow_redirects=False).headers["location"] == "/"


def test_restart_keeps_users_and_data(db):
    """Перезапуск/обновление контейнера = повторный lifespan: миграции, группы, admin по умолчанию."""
    db.add(Board(key="HL", name="Homelab"))
    users_group = db.scalar(select(Group).where(Group.name == "Пользователи"))
    db.execute(delete(GroupPermission).where(GroupPermission.group_id == users_group.id,
                                             GroupPermission.perm == "jira.edit"))
    db.commit()
    users_before = sorted(db.scalars(select(User.username)))
    for _ in range(2):
        with TestClient(app):
            pass
    db.expire_all()
    assert sorted(db.scalars(select(User.username))) == users_before
    assert db.scalar(select(User).where(User.must_change_credentials.is_(True))) is None
    assert db.scalar(select(func.count()).select_from(Board)) == 1
    perms = set(db.scalars(select(GroupPermission.perm).where(GroupPermission.group_id == users_group.id)))
    assert "jira.edit" not in perms  # изменённые администратором права не сбрасываются
    _login("admin", PASSWORD)  # пароли не сброшены
