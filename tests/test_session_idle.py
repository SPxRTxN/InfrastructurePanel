"""Тайм-аут бездействия: 30 минут по умолчанию, фоновые опросы не продлевают, в режиме обновления админа не выкидывает."""
from datetime import timedelta

from sqlalchemy import select

from app.models import User, UserSession
from app.services.settings_registry import get_setting, set_setting
from app.utils import utcnow


def _session(db, username):
    db.expire_all()
    return db.scalar(select(UserSession).join(User).where(User.username == username))


def _age(db, username, minutes):
    s = _session(db, username)
    s.last_seen = utcnow() - timedelta(minutes=minutes)
    db.commit()


def test_default_is_30_minutes(db):
    assert get_setting(db, "security.idle_minutes") == 30


def test_idle_session_expires(user, db):
    assert user.get("/").status_code == 200
    _age(db, "user", 31)
    r = user.get("/", follow_redirects=False)
    assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]
    assert _session(db, "user") is None


def test_activity_keeps_session_but_background_poll_does_not(user, db):
    _age(db, "user", 20)
    assert user.get("/").status_code == 200  # действие человека — продлевает
    assert _session(db, "user").last_seen > utcnow() - timedelta(minutes=1)
    _age(db, "user", 20)
    assert user.get("/api/notifications", headers={"X-Background": "1"}).status_code == 200
    assert _session(db, "user").last_seen < utcnow() - timedelta(minutes=19)  # опрос не продлил
    _age(db, "user", 31)
    assert user.get("/api/notifications", headers={"X-Background": "1"}).status_code == 401


def test_admin_not_logged_out_during_update(admin, user, db, monkeypatch):
    from app.services import maintenance

    monkeypatch.setattr(maintenance, "is_on", lambda db: True)
    _age(db, "admin", 120)
    assert admin.get("/settings/update").status_code == 200  # админ следит за обновлением
    _age(db, "user", 120)
    assert _session(db, "user") is not None
    user.get("/", follow_redirects=False)
    assert _session(db, "user") is None  # обычного пользователя — выкидывает


def test_zero_disables(user, db):
    set_setting(db, "security.idle_minutes", 0)
    _age(db, "user", 600)
    assert user.get("/").status_code == 200
