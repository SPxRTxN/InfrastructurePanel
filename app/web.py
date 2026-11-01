"""Общее для всех роутеров: шаблоны, текущий пользователь, проверка прав и CSRF."""
import json
import re
from datetime import timedelta
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, Response
from starlette.requests import HTTPConnection
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import BASE_DIR, config
from app import __version__
from app.db import get_db
from app.models import Notification, User, UserSession
from app.services.markdown import render_markdown
from app.services.security import hash_token, tokens_equal
from app.services.settings_registry import MODULES, get_setting
from app.utils import utcnow

templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def human_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def percent(part: int | None, total: int | None) -> int:
    return round(part * 100 / total) if part is not None and total else 0


def markdown_filter(text: str) -> Markup:
    # Markup безопасен: render_markdown санитизирует результат через nh3 (аудит A-14)
    return Markup(render_markdown(text))  # nosec B704


templates.env.filters["bytes"] = human_bytes
templates.env.filters["markdown"] = markdown_filter
templates.env.globals["percent"] = percent
templates.env.globals["app_version"] = __version__
templates.env.globals["project_dir"] = config.project_dir  # путь на хосте для команд-подсказок (PROJECT_DIR)

SESSION_COOKIE = "hl_session"
# CSRF для формы входа: сессии ещё нет, поэтому double-submit cookie
LOGIN_CSRF_COOKIE = "hl_login_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# (url, иконка, название, право). Главная видна всем вошедшим.
MENU = [
    ("/", "house", "Главная", None),
    ("/jira", "square-kanban", "Jira", "jira.view"),
    ("/confluence", "book-open", "Confluence", "confluence.view"),
    ("/calendar", "calendar", "Календарь", "calendar.view"),
]
# «Инфраструктура» — один пункт меню с вкладками: (url, иконка, название, право)
INFRA_TABS = [
    ("/servers", "server", "Серверы", "servers.view"),
    ("/nodes", "cpu", "Узлы", "servers.view"),
    ("/agents", "refresh-cw", "Агенты", "servers.view"),
    ("/topology", "network", "Карта сети", "topology.view"),
]
# Оповещения, VPN и архив — разделы настроек панели (адреса и права прежние)
SETTINGS_PAGES = {"/alerts", "/vpn", "/archive"}


def _menu(user: User) -> list[tuple]:
    items = [m for m in MENU if m[3] is None or user.can(m[3])]
    tabs = [t for t in INFRA_TABS if user.can(t[3])]
    if tabs:
        items.insert(1, (tabs[0][0], "server", "Инфраструктура", None))
    return items


class LoginRequired(Exception):
    pass


class SetupRequired(Exception):
    """Вошёл администратор по умолчанию: всё, кроме /setup, закрыто до смены логина и пароля."""


SETUP_PATH = "/setup"


def load_session(request: Request, db: Session) -> UserSession | None:
    """Находит сессию по cookie; результат кэшируется на время запроса."""
    if hasattr(request.state, "session_row"):
        return request.state.session_row
    row = None
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
        if row and (row.expires_at < utcnow() or not row.user.is_active):
            row = None
    request.state.session_row = row
    return row


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    row = load_session(request, db)
    if row is None:
        raise LoginRequired()
    # Проверка здесь, а не в отдельных роутерах: current_user — зависимость каждого обработчика
    if row.user.must_change_credentials and request.url.path != SETUP_PATH:
        raise SetupRequired()
    enabled = set(get_setting(db, "modules.enabled"))
    row.user.disabled_modules = frozenset(m for m, _ in MODULES if m not in enabled)
    _check_idle(request, db, row)
    return row.user


IDLE_TOUCH = timedelta(seconds=60)  # last_seen пишем не чаще раза в минуту — без записи в БД на каждый запрос


def _check_idle(request: Request, db: Session, row: UserSession) -> None:
    """Тайм-аут бездействия (security.idle_minutes). Фоновые опросы (X-Background) активностью не считаются.
    Во время обновления панели администратора не выкидываем — он следит за ходом обновления."""
    now = utcnow()
    idle = get_setting(db, "security.idle_minutes")
    last = row.last_seen or row.created_at
    if idle and now - last > timedelta(minutes=idle):
        from app.services import maintenance

        if not (row.user.can("settings.manage") and maintenance.is_on(db)):
            db.delete(row)
            db.commit()
            request.state.session_row = None
            raise LoginRequired()
    if request.headers.get("x-background") != "1" and (row.last_seen is None or now - row.last_seen > IDLE_TOUCH):
        row.last_seen = now
        db.commit()


def require(perm: str):
    """Зависимость: пользователь вошёл и имеет право perm, иначе 403."""

    def dep(user: User = Depends(current_user)) -> User:
        if not user.can(perm):
            raise HTTPException(403, "Недостаточно прав")
        return user

    return dep


async def csrf_protect(conn: HTTPConnection, db: Session = Depends(get_db)) -> None:
    """Глобальная зависимость: каждый изменяющий запрос несёт CSRF-токен
    в заголовке X-CSRF-Token (HTMX/fetch) или в поле формы csrf_token.
    WebSocket агентов (`/agent/ws`) — не браузерный запрос с cookie: там вход по токену узла."""
    if conn.scope["type"] != "http":
        return
    # Для HTTP FastAPI передаёт сюда тот же объект Request, что и обработчику (тело формы кэшируется в нём)
    request: Request = conn  # type: ignore[assignment]
    if request.method in SAFE_METHODS:
        return
    sent = request.headers.get("x-csrf-token")
    if not sent and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        sent = (await request.form()).get("csrf_token")
    if request.url.path == "/login":
        expected = request.cookies.get(LOGIN_CSRF_COOKIE)
    else:
        row = load_session(request, db)
        expected = row.csrf_token if row else None
    if not tokens_equal(sent, expected):
        raise HTTPException(403, "Неверный CSRF-токен")


def render(request: Request, name: str, ctx: dict | None = None, user: User | None = None,
           db: Session | None = None, status_code: int = 200):
    """Рендер страницы с общим контекстом layout (меню, тема, CSRF, уведомления)."""
    context = dict(ctx or {})
    context["user"] = user
    if user is not None and db is not None:
        row = load_session(request, db)
        context["csrf_token"] = row.csrf_token if row else ""
        context["menu"] = _menu(user)
        context["infra_tabs"] = [t for t in INFRA_TABS if user.can(t[3])]
        context["ui"] = {
            "theme": get_setting(db, "appearance.theme", user.id),
            "accent": get_setting(db, "appearance.accent", user.id),
            "density": get_setting(db, "appearance.density", user.id),
            "sidebar_collapsed": get_setting(db, "appearance.sidebar_collapsed", user.id),
        }
        context["unread_count"] = db.scalar(
            select(func.count()).select_from(Notification)
            .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        )
    context.setdefault("ui", {"theme": "dark", "accent": "pink", "density": "comfortable",
                              "sidebar_collapsed": False})
    path = request.url.path
    context["active"] = "/" if path == "/" else "/" + path.strip("/").split("/")[0]
    context["active_tab"] = context["active"]
    if context["active"] in {t[0] for t in INFRA_TABS} and context.get("infra_tabs"):
        # Подсветка пункта «Инфраструктура», какая бы вкладка ни была открыта
        context["active"] = context["infra_tabs"][0][0]
    elif context["active"] in SETTINGS_PAGES:
        context["active"] = "/settings"
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def json_download(payload: dict | bytes, name: str) -> Response:
    """JSON файлом: имя — латиница для старых браузеров + исходное (кириллица) через filename*."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, indent=1).encode()
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "export"
    disposition = (f'attachment; filename="{ascii_name}-{stamp}.json"; '
                   f"filename*=UTF-8''{quote(name)}-{stamp}.json")
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": disposition, "Cache-Control": "no-store"})


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"
