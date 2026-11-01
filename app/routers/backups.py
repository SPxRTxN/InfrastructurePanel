"""«Настройки → Бэкапы»: расписание, последний бэкап, проверка восстановления, журнал. Право — `settings.manage`.

Роутер подключается раньше `settings`: иначе общий `/settings/{section}` перехватит адрес.
"""
import asyncio
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import config
from app.db import get_db
from app.models import BackupLog, User
from app.routers.settings import _page
from app.services import audit, backup
from app.services.settings_registry import REGISTRY, coerce, get_setting, set_setting
from app.web import require

router = APIRouter(prefix="/settings/backups")


def _view(request: Request, db: Session, user: User, error: str = "", status_code: int = 200):
    out = backup.backup_dir()
    ok = backup.last_backup(db, ok=True)
    return _page(request, db, user, "settings/backups.html", "backups", status_code=status_code, error=error,
                 backup_dir=str(out) if out else "", configured=bool(out), age=bool(config.backup_age_recipient),
                 encrypted=bool(config.db_encryption_key), last=backup.last_backup(db), last_ok=ok,
                 verify=backup.last_verify(db), next_run=backup.next_run(db),
                 s={k: get_setting(db, f"backups.{k}") for k in ("enabled", "hour", "keep")},
                 rows=db.scalars(select(BackupLog).order_by(BackupLog.id.desc()).limit(50)).all(),
                 flash=request.query_params.get("flash", "")[:300])


@router.get("")
def backups_page(request: Request, user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    return _view(request, db, user)


@router.post("")
async def backups_save(request: Request, user: User = Depends(require("settings.manage")),
                       db: Session = Depends(get_db)):
    form = await request.form()
    try:
        values = {"enabled": coerce(REGISTRY["backups.enabled"], form.get("enabled") == "on"),
                  "hour": coerce(REGISTRY["backups.hour"], form.get("hour")),
                  "keep": coerce(REGISTRY["backups.keep"], form.get("keep"))}
    except ValueError as e:
        return _view(request, db, user, str(e), 400)
    for k, v in values.items():
        set_setting(db, f"backups.{k}", v)
    audit.log_event(db, "backup_config", request=request, username=user.username,
                    detail=f"бэкапы: включено={values['enabled']}, час={values['hour']}, копий={values['keep']}")
    return RedirectResponse("/settings/backups?saved=1", 303)


@router.post("/run")
async def backup_run(request: Request, user: User = Depends(require("settings.manage"))):
    # Принудительно с проверкой восстановления: кнопка нажимается редко, а копия и так делается целиком
    res = await asyncio.to_thread(backup.run_backup, "manual", True)
    if res.ok:
        v = res.verify
        msg = f"Бэкап сделан: {res.path.name} ({res.size // 1024} КБ). Проверка восстановления: " + \
              (("успешно" if v.ok else "НЕ ПРОШЛА — " + v.detail) if v else "не выполнялась")
    else:
        msg = "Бэкап не удался: " + res.error
    return RedirectResponse("/settings/backups?flash=" + quote(msg), 303)


@router.post("/verify")
async def backup_verify(request: Request, user: User = Depends(require("settings.manage"))):
    res = await asyncio.to_thread(backup.run_verify, "manual")
    msg = ("Восстановление проверено: " if res.ok else "Проверка не прошла: ") + res.detail
    return RedirectResponse("/settings/backups?flash=" + quote(msg), 303)
