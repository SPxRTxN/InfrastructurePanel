"""«Настройки → Записи консоли»: список, проигрыватель, скачивание .cast, закрепление, удаление (`settings.manage`).

Роутер подключается раньше `settings`: иначе общий `/settings/{section}` перехватит адрес.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ConsoleRecording, Server, User
from app.routers.settings import _page
from app.services import audit, recording
from app.web import require

router = APIRouter(prefix="/settings/recordings")


def _rec(db: Session, rec_id: int) -> ConsoleRecording:
    rec = db.get(ConsoleRecording, rec_id)
    if not rec:
        raise HTTPException(404, "Запись не найдена")
    return rec


def _seconds(rec: ConsoleRecording) -> int:
    return max(0, round((rec.ended_at - rec.started_at).total_seconds()))


@router.get("")
def recordings_page(request: Request, server: int = 0, user: User = Depends(require("settings.manage")),
                    db: Session = Depends(get_db)):
    rows = [{"r": r, "seconds": _seconds(r)} for r in recording.listing(db, server or None)]
    servers = db.query(Server.id, Server.name).order_by(Server.name).all()
    return _page(request, db, user, "settings/recordings.html", "recordings", rows=rows, servers=servers,
                 server_id=server)


@router.get("/{rec_id}")
def recording_page(rec_id: int, request: Request, user: User = Depends(require("settings.manage")),
                   db: Session = Depends(get_db)):
    rec = _rec(db, rec_id)
    audit.log_event(db, "recording_view", request=request, username=user.username,
                    detail=f"запись #{rec.id}: {rec.server_name}, {rec.username}")
    return _page(request, db, user, "settings/recording.html", "recordings", rec=rec, seconds=_seconds(rec))


@router.get("/{rec_id}/cast")
def recording_cast(rec_id: int, request: Request, play: int = 0, user: User = Depends(require("settings.manage")),
                   db: Session = Depends(get_db)):
    """play=1 — данные для проигрывателя на странице записи (просмотр уже в журнале); иначе — скачивание."""
    rec = _rec(db, rec_id)
    body = recording.cast_text(rec)
    if play:
        return Response(body, media_type="text/plain; charset=utf-8")
    audit.log_event(db, "recording_download", request=request, username=user.username,
                    detail=f"запись #{rec.id}: {rec.server_name}, {rec.username}")
    return Response(body, media_type="application/x-asciicast",
                    headers={"Content-Disposition": f'attachment; filename="console-{rec.id}.cast"'})


@router.post("/{rec_id}/pin")
def recording_pin(rec_id: int, user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    rec = _rec(db, rec_id)
    rec.pinned = not rec.pinned
    db.commit()
    return RedirectResponse(f"/settings/recordings/{rec.id}", 303)


@router.post("/{rec_id}/delete")
def recording_delete(rec_id: int, request: Request, user: User = Depends(require("settings.manage")),
                     db: Session = Depends(get_db)):
    rec = _rec(db, rec_id)
    audit.log_event(db, "recording_delete", request=request, username=user.username,
                    detail=f"запись #{rec.id}: {rec.server_name}, {rec.username}", commit=False)
    db.delete(rec)
    db.commit()
    return RedirectResponse("/settings/recordings", 303)
