"""Закрытые пункты аудита 2026-10-29: A-01 (баннеры, редиректы), A-02 (лимит на IP), A-07 (сроки), A-10 (сессии)."""
import asyncio
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.models import Event, Notification, ReminderLog, User, UserSession
from app.services import audit, monitoring
from app.utils import utcnow
from app.web import LOGIN_CSRF_COOKIE, SESSION_COOKIE
from tests.conftest import PASSWORD, login


def _csrf(client):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)


def test_ip_limit_across_usernames(db):
    """A-02: 20 неудач с одного IP по разным логинам — дальше 429 даже с верным паролем."""
    client = TestClient(app)
    for i in range(20):
        client.post("/login", data={"username": f"nobody{i}", "password": "x", "csrf_token": _csrf(client)})
    r = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": _csrf(client)},
                    follow_redirects=False)
    assert r.status_code == 429


def test_password_change_rotates_current_session(admin, db):
    """A-10: после смены пароля старая cookie этого же браузера не работает."""
    old = admin.cookies.get(SESSION_COOKIE)
    r = admin.post("/settings/profile/password", data={"old_password": PASSWORD, "new_password": "new-password-1",
                                                       "new_password2": "new-password-1"}, follow_redirects=False)
    assert r.status_code == 303
    new = admin.cookies.get(SESSION_COOKIE)
    assert new and new != old
    stale = TestClient(app, cookies={SESSION_COOKIE: old})
    assert stale.get("/", follow_redirects=False).status_code in (302, 303, 307)
    assert admin.get("/").status_code == 200


def test_login_replaces_previous_browser_session(db):
    client = login(TestClient(app), "viewer")
    # Вход другим пользователем из того же браузера (вторая вкладка): форма входа — double-submit cookie
    client.cookies.set(LOGIN_CSRF_COOKIE, "same-token")
    client.headers.pop("X-CSRF-Token")  # заголовок сессии viewer важнее поля формы
    r = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": "same-token"},
                    follow_redirects=False)
    assert r.status_code == 303
    sessions = db.scalars(select(UserSession)).all()
    assert [s.user.username for s in sessions] == ["admin"]


def test_cleanup_notifications_reminders_sessions(db):
    """A-07: прочитанные уведомления и отметки напоминаний — 30 дней, просроченные сессии — сразу."""
    admin = db.scalar(select(User).where(User.username == "admin"))
    old = utcnow() - timedelta(days=31)
    db.add_all([Notification(user_id=admin.id, title="old read", is_read=True, created_at=old),
                Notification(user_id=admin.id, title="old unread", is_read=False, created_at=old),
                Notification(user_id=admin.id, title="fresh read", is_read=True)])
    event = Event(title="e", start=utcnow(), end=utcnow())
    db.add(event)
    db.flush()
    db.add(ReminderLog(event_id=event.id, occurrence=old, sent_at=old))
    db.add(UserSession(token_hash="t" * 64, csrf_token="c", user_id=admin.id, expires_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    audit.cleanup(db)
    assert sorted(n.title for n in db.scalars(select(Notification))) == ["fresh read", "old unread"]
    assert db.scalar(select(func.count()).select_from(ReminderLog)) == 0
    assert db.scalar(select(func.count()).select_from(UserSession)) == 0


def test_foreign_banner_not_shown():
    """A-01: на порт не-SSH панель говорит лишь «не SSH», без строки чужого сервиса."""
    async def run():
        async def handle(reader, writer):
            writer.write(b"+OK redis 7.2.4 secret-banner\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            return await monitoring.ssh_banner("127.0.0.1", port)

    ok, text = asyncio.run(run())
    assert ok is False and "secret" not in text and "redis" not in text


def test_http_check_does_not_follow_redirects():
    """A-01: 3xx — сервис жив, но на адрес из Location проверка не идёт."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(301)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        ok, text = monitoring._http_get(f"http://127.0.0.1:{httpd.server_port}/")
    finally:
        httpd.shutdown()
    assert ok is True and text == "HTTP 301" and hits == ["/"]
