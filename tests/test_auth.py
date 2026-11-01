import re

from sqlalchemy import select

from app.models import LoginAttempt, User, UserSession
from tests.conftest import PASSWORD, login


def _login_token(client):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)


def test_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_api_requires_login(client):
    assert client.get("/api/notifications").status_code == 401


def test_login_success_sets_secure_cookie(client, db):
    token = _login_token(client)
    r = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("hl_session=")][0].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    raw = cookie.split(";")[0].split("=", 1)[1]
    # в БД хранится хеш, а не сам токен
    assert not db.scalar(select(UserSession).where(UserSession.token_hash == raw))
    assert db.scalar(select(LoginAttempt).where(LoginAttempt.success.is_(True)))


def test_password_hash_is_argon2id(db):
    assert db.scalar(select(User).where(User.username == "admin")).password_hash.startswith("$argon2id$")


def test_wrong_password_logged_and_not_echoed(client, db):
    token = _login_token(client)
    r = client.post("/login", data={"username": "admin", "password": "secret-wrong-xyz", "csrf_token": token})
    assert r.status_code == 401
    assert "secret-wrong-xyz" not in r.text
    att = db.scalar(select(LoginAttempt))
    assert att.success is False and att.reason == "bad_credentials"


def test_login_without_csrf_rejected(client):
    client.get("/login")
    r = client.post("/login", data={"username": "admin", "password": PASSWORD})
    assert r.status_code == 403


def test_bruteforce_lockout(client):
    for _ in range(5):
        r = client.post("/login", data={"username": "admin", "password": "bad", "csrf_token": _login_token(client)})
        assert r.status_code == 401
    # даже верный пароль не пускает, пока действует блокировка
    r = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": _login_token(client)})
    assert r.status_code == 429
    # другой пользователь с того же IP не заблокирован
    r = client.post("/login", data={"username": "user", "password": PASSWORD, "csrf_token": _login_token(client)},
                    follow_redirects=False)
    assert r.status_code == 303


def test_open_redirect_blocked(client):
    r = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": _login_token(client),
                                    "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_logout(admin):
    r = admin.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert admin.get("/", follow_redirects=False).status_code == 303


def test_csrf_required_for_post(admin):
    token = admin.headers.pop("X-CSRF-Token")
    assert admin.post("/settings/sidebar").status_code == 403
    assert admin.post("/settings/sidebar", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert admin.post("/settings/sidebar", headers={"X-CSRF-Token": token}).status_code == 204


def test_inactive_user_cannot_login(client, db):
    u = db.scalar(select(User).where(User.username == "user"))
    u.is_active = False
    db.commit()
    r = client.post("/login", data={"username": "user", "password": PASSWORD, "csrf_token": _login_token(client)})
    assert r.status_code == 401


def test_security_headers(client):
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"]


def test_change_password_kills_other_sessions(admin, client):
    other = login(client, "admin")
    r = admin.post("/settings/profile/password", data={
        "old_password": PASSWORD, "new_password": "new-password-456", "new_password2": "new-password-456"},
        follow_redirects=False)
    assert r.status_code == 303
    assert other.get("/", follow_redirects=False).status_code == 303
    assert admin.get("/").status_code == 200
