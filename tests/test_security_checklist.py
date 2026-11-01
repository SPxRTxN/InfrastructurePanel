"""Чек-лист безопасности (раздел 11 задания): по тесту на пункт, где это проверяемо автоматически."""
import logging
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import config
from app.main import app
from app.models import Server, User
from app.web import current_user
from tests.conftest import PASSWORD, login

APP_DIR = Path(__file__).resolve().parent.parent / "app"
# /agent/releases/…: выдача архива агенту — вход не по сессии, а по токену узла (Bearer), без него 401
PUBLIC = {"/login", "/logout", "/healthz", "/agent/releases/{filename}"}


def _routes():
    # В FastAPI 0.16x подключённые роутеры хранятся обёрткой с original_router, а не плоским списком
    out = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif hasattr(r, "original_router"):
            out += [x for x in r.original_router.routes if isinstance(x, APIRoute)]
    assert len(out) > 50
    return out


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _depends_on(dependant, target) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


# 1. Пароли argon2id, не попадают в логи и ответы API
def test_passwords_not_in_logs_or_responses(client, admin, db, caplog):
    caplog.set_level(logging.DEBUG)
    long_pw = "x" * 2000  # 422 от валидации не должен возвращать введённое значение
    client.cookies.set("hl_login_csrf", "t")
    r = client.post("/login", data={"username": "admin", "password": long_pw, "csrf_token": "t"})
    assert r.status_code == 422 and long_pw not in r.text
    login(client, "user")
    assert PASSWORD not in caplog.text
    for url in ["/settings/users", "/api/servers", "/settings/profile"]:
        body = admin.get(url).text
        assert PASSWORD not in body and "$argon2" not in body
    assert db.scalar(select(User)).password_hash.startswith("$argon2id$")


# 2. Изменяющие запросы — не GET и с CSRF
def test_all_mutating_routes_require_csrf(admin):
    token = admin.headers.pop("X-CSRF-Token")
    checked = 0
    for route in _routes():
        for method in route.methods - {"GET", "HEAD"}:
            if route.path == "/login":
                continue
            r = admin.request(method, _concrete(route.path), follow_redirects=False)
            assert r.status_code == 403, f"{method} {route.path} без CSRF → {r.status_code}"
            checked += 1
    assert checked > 30
    admin.headers["X-CSRF-Token"] = token


def test_get_routes_do_not_mutate():
    # Изменяющие действия не должны быть GET: имена обработчиков GET — только просмотр
    bad = [r.path for r in _routes() if "GET" in r.methods
           and re.search(r"(create|update|delete|save|move|toggle|restore|link|logout)", r.endpoint.__name__)]
    assert bad == []


# 3. Права проверяются на сервере в каждом обработчике
def test_every_route_requires_login():
    missing = [r.path for r in _routes() if r.path not in PUBLIC and not _depends_on(r.dependant, current_user)]
    assert missing == []


def test_anonymous_gets_nothing(client):
    for route in _routes():
        if route.path in PUBLIC or "GET" not in route.methods:
            continue
        r = client.get(_concrete(route.path), follow_redirects=False)
        assert r.status_code in (303, 401), f"{route.path} → {r.status_code}"


def test_user_without_permissions_gets_403(client, db):
    u = User(username="nobody", password_hash=db.scalar(select(User)).password_hash)
    db.add(u)
    db.commit()
    c = login(client, "nobody")
    for path in ["/servers", "/jira", "/confluence", "/calendar", "/topology", "/api/servers",
                 "/api/topology", "/api/calendar/events?start=2026-01-01&end=2026-02-01", "/settings/users"]:
        assert c.get(path).status_code == 403, path
    # главная и собственные настройки доступны
    assert c.get("/").status_code == 200 and c.get("/settings/profile").status_code == 200


# 4. Markdown санитизируется — см. test_jira.py::test_markdown_sanitized и test_confluence.py::test_page_xss_sanitized

# 5. SQL только через ORM/параметры
def test_no_raw_sql_strings():
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        src = path.read_text()
        if re.search(r"from sqlalchemy import[^\n]*\btext\b", src) or re.search(r"\.execute\(\s*f[\"']", src):
            offenders.append(path.name)
        if re.search(r"(select|insert|update|delete)\s.*%s|\.format\(.*(SELECT|INSERT|UPDATE|DELETE)", src, re.I):
            offenders.append(path.name)
    assert offenders == []


# 6. Заголовки безопасности — на всех типах ответов
@pytest.mark.parametrize("path", ["/login", "/healthz", "/static/css/app.css", "/nope", "/api/notifications"])
def test_security_headers_everywhere(client, path):
    h = client.get(path).headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert "script-src" not in h["content-security-policy"] or "unsafe" not in h["content-security-policy"].split("script-src")[1]
    assert h["x-frame-options"] == "DENY"
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "same-origin"


def test_no_inline_scripts_in_templates():
    for tpl in (APP_DIR / "templates").rglob("*.html"):
        src = tpl.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)", src), tpl.name
        assert not re.search(r"\son[a-z]+=", src), tpl.name  # onclick= и т.п. запрещены CSP


# 7. Путь к SSH-ключу и секреты не отдаются в UI/API
def test_secrets_not_exposed(admin, db):
    admin.post("/servers", data={"name": "Pi", "host": "10.0.0.2", "collector": "ssh",
                                 "ssh_key_path": "/root/.ssh/super_secret_key"})
    pages = ["/", "/servers", "/api/servers", "/settings/intervals", "/api/topology", "/topology",
             f"/servers/{db.scalar(select(Server)).id}", f"/servers/{db.scalar(select(Server)).id}/settings"]
    for url in pages:
        body = admin.get(url).text
        assert "super_secret_key" not in body and config.secret_key not in body, url


def test_secure_cookie_flag(monkeypatch):
    monkeypatch.setattr(config, "cookie_secure", True)
    client = TestClient(app, base_url="https://testserver")  # Secure-cookie браузер шлёт только по HTTPS
    r = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    r = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": token},
                    follow_redirects=False)
    cookie = [h for h in r.headers.get_list("set-cookie") if h.startswith("hl_session=")][0]
    assert "Secure" in cookie and "HttpOnly" in cookie


# 8. Блокировка перебора и журнал входов — см. test_auth.py::test_bruteforce_lockout; журнал виден администратору
def test_login_journal_visible_to_admin(client, admin):
    client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"username": "viewer", "password": "bad", "csrf_token": token})
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    client.post("/login", data={"username": "ghost", "password": "bad", "csrf_token": token})
    html = admin.get("/settings/users").text
    assert "<td>viewer</td>" in html and "неверные данные" in html
    # Несуществующий логин не хранится: туда по ошибке набирают пароль (аудит S-06)
    assert "<td>ghost" not in html and "неизвестный логин" in html
