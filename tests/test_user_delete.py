"""Удаление пользователей в Настройки → Пользователи."""
from sqlalchemy import func, select

from app.models import Board, BoardColumn, Issue, LoginAttempt, Notification, SettingValue, User, UserSession
from app.models.auth import user_groups
from tests.conftest import login


def _uid(db, name):
    return db.scalar(select(User.id).where(User.username == name))


def test_delete_user_cleans_related_rows(admin, db, client):
    uid = _uid(db, "user")
    login(client, "user")  # у пользователя есть сессия
    board = Board(key="HL", name="Homelab")
    col = BoardColumn(board=board, name="To Do", position=0)
    db.add_all([board, col,
                Issue(board=board, column=col, number=1, title="t", assignee_id=uid),
                Notification(user_id=uid, title="n"),
                SettingValue(key="appearance.theme", user_id=uid, value='"light"')])
    db.commit()

    r = admin.post(f"/settings/users/{uid}/delete", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(User, uid) is None
    assert db.scalar(select(func.count()).select_from(user_groups).where(user_groups.c.user_id == uid)) == 0
    for model in (UserSession, Notification, SettingValue):
        assert db.scalar(select(func.count()).select_from(model).where(model.user_id == uid)) == 0, model
    assert db.scalar(select(Issue)).assignee_id is None  # задача осталась, исполнитель пуст
    assert db.scalar(select(func.count()).select_from(LoginAttempt).where(LoginAttempt.username == "user"))
    assert client.get("/", follow_redirects=False).headers["location"].startswith("/login")  # сессия умерла
    assert "<b>user</b>" not in admin.get("/settings/users").text


def test_cannot_delete_self(admin, db):
    r = admin.post(f"/settings/users/{_uid(db, 'admin')}/delete")
    assert r.status_code == 400 and "самого себя" in r.text
    assert _uid(db, "admin")


def test_delete_requires_users_manage(user, viewer, db):
    target = _uid(db, "viewer")
    assert user.post(f"/settings/users/{target}/delete").status_code == 403
    assert viewer.post(f"/settings/users/{_uid(db, 'user')}/delete").status_code == 403
    assert _uid(db, "viewer") and _uid(db, "user")


def test_delete_unknown_user_404(admin):
    assert admin.post("/settings/users/99999/delete").status_code == 404


def test_delete_button_not_shown_for_self(admin, db):
    html = admin.get("/settings/users").text
    assert f"/settings/users/{_uid(db, 'user')}/delete" in html
    assert f"/settings/users/{_uid(db, 'admin')}/delete" not in html
