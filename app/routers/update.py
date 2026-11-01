"""«Настройки → Обновление»: запуск обновления панели через хост-службу и ход выполнения. Право — `settings.manage`.

Роутер подключается раньше `settings`: иначе общий `/settings/{section}` перехватит адрес.
"""
import app as app_pkg
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.routers.settings import _page
from app.services import maintenance, releases
from app.web import require

router = APIRouter(prefix="/settings/update")


def _state(db: Session) -> dict:
    row = maintenance.get(db)
    st = maintenance.read_status()
    on = bool(row and row.enabled)
    return {"active": on, "status": st, "until": maintenance.local_hhmm(row.eta) if on else "",
            "version": row.target_version if on else "",
            # Отменить можно, пока служба не взяла запрос
            "can_cancel": bool(on and st and st["state"] == "requested"),
            "steps": maintenance.STEPS, "log": maintenance.read_log()}


def _view(request: Request, db: Session, user: User, error: str = "", status_code: int = 200):
    d = maintenance.update_dir()
    state = _state(db)
    # «active» в контексте страниц занят подсветкой меню (render перезаписывает) — флаг обновления под своим именем.
    # Иначе шаблон всегда считал обновление идущим, а update.js по кругу перезагружал страницу.
    state["update_active"] = state.pop("active")
    return _page(request, db, user, "settings/update.html", "update", status_code=status_code, error=error,
                 current=getattr(app_pkg, "__version__", "") or "неизвестна", eta_min=round(maintenance.eta_seconds() / 60),
                 update_dir=str(d) if d else "", updater=maintenance.updater_info(),
                 releases=releases.available(db), **state)


@router.get("")
def panel_version_page(request: Request, user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    return _view(request, db, user)


@router.get("/status")
def progress_json(user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    """Опрос раз в 2 с из app/static/js/update.js."""
    s = _state(db)
    return JSONResponse(s, headers={"Cache-Control": "no-store"})


@router.post("/releases/refresh")
def releases_refresh(user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    """«Проверить обновления»: перечитать список релизов, не дожидаясь кэша."""
    releases.available(db, force=True)
    return RedirectResponse("/settings/update", 303)


@router.post("/start")
def update_start(request: Request, version: str = Form(""), user: User = Depends(require("settings.manage")),
                 db: Session = Depends(get_db)):
    try:
        maintenance.start_update(db, user.username, version)
    except ValueError as e:
        return _view(request, db, user, str(e), 400)
    return RedirectResponse("/settings/update", 303)


@router.post("/cancel")
def update_cancel(request: Request, user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    if not maintenance.cancel(db, user.username):
        return _view(request, db, user, "Отменить уже нельзя: служба обновления взяла запрос", 400)
    return RedirectResponse("/settings/update?saved=1", 303)
