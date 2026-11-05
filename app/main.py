import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import BASE_DIR, config
from app.db import SessionLocal, check_db_encryption, secure_db_files
from app.routers import (agent_install, agent_updates, alerts, archive, auth, backups, calendar, confluence, console, home,
                         jira, markdown, nodes, notifications, recordings, servers, services, settings, topology, transfer,
                         update, vpn)
from app.services import agent_updates as agent_updates_service
from app.services import maintenance
from app.services.permissions import ensure_default_admin, ensure_default_groups
from app.web import LoginRequired, SetupRequired, csrf_protect, current_user, render

CSP = (
    "default-src 'self'; "
    # FullCalendar и Chart.js выставляют inline-стили из JS; скрипты — строго 'self'
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


class StripQueryFromAccessLog(logging.Filter):
    """Access-лог uvicorn без query-строки: поиск Confluence и фильтры Jira не оседают в логах (аудит S-09)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app import migrate
    from app.services import guide, jobs, scheduler  # noqa: F401 — jobs регистрирует задачи

    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, StripQueryFromAccessLog) for f in access_log.filters):
        access_log.addFilter(StripQueryFromAccessLog())
    check_db_encryption()
    migrate.upgrade()
    secure_db_files()
    with SessionLocal() as db:
        ensure_default_groups(db)
        if ensure_default_admin(db):
            pw = "пароль из FIRST_ADMIN_PASSWORD" if config.first_admin_password else "пароль admin"
            logging.getLogger("uvicorn.error").warning(
                f"Первый запуск: создан администратор admin ({pw}) — войдите и смените логин и пароль")
    guide.sync_safely()  # руководство в Confluence; ошибка внутри только в лог
    maintenance.check_safely()  # итог обновления панели от хост-службы → выключить режим обслуживания
    agent_updates_service.register_bundled_safely()  # агент из образа → «доступно обновление» у узлов
    task = scheduler.start() if config.scheduler_enabled else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Homelab Dashboard", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan, dependencies=[Depends(csrf_protect)])
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")

# transfer и backups — раньше settings: иначе общий /settings/{section} перехватит их адреса
for r in (auth, home, notifications, transfer, backups, update, recordings, settings, servers, console, nodes, agent_updates, agent_install, alerts, vpn,
          archive, jira,
          confluence, calendar, topology, services, markdown):
    app.include_router(r.router)


# Раньше security_headers: добавленный позже оборачивает внешним слоем, и заголовки попадают и на страницу 503
app.middleware("http")(maintenance.middleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    if config.cookie_secure:
        # Только за HTTPS: по HTTP браузер HSTS игнорирует, а включённый по ошибке он запер бы сайт на https
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if not request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api") or "application/json" in request.headers.get("accept", "")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Стандартный ответ FastAPI включает "input" — так пароль из формы мог бы вернуться в ответе
    errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, 422)


@app.exception_handler(LoginRequired)
async def login_required_handler(request: Request, _exc: LoginRequired):
    if _wants_json(request) or request.headers.get("hx-request"):
        return JSONResponse({"detail": "Требуется вход"}, 401, headers={"HX-Redirect": "/login"})
    return RedirectResponse(f"/login?next={request.url.path}", 303)


@app.exception_handler(SetupRequired)
async def setup_required_handler(request: Request, _exc: SetupRequired):
    if _wants_json(request) or request.headers.get("hx-request"):
        return JSONResponse({"detail": "Смените логин и пароль администратора"}, 403,
                            headers={"HX-Redirect": "/setup"})
    return RedirectResponse("/setup", 303)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if _wants_json(request) or request.headers.get("hx-request") or exc.status_code not in (400, 403, 404):
        return await http_exception_handler(request, exc)
    # Страница ошибки для обычной навигации и отправки форм, с layout если пользователь вошёл
    if hasattr(request.state, "session_row"):
        del request.state.session_row  # сессия БД запроса уже закрыта, ищем заново
    with SessionLocal() as db:
        try:
            user = current_user(request, db)
        except (LoginRequired, SetupRequired):
            user = None
        return render(request, "error.html", {"title": f"Ошибка {exc.status_code}", "code": exc.status_code,
                                              "message": exc.detail}, user, db if user else None,
                      status_code=exc.status_code)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}
