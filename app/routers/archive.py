"""Архив удалённого: «Инфраструктура → Архив». Снимки с историей хранятся `history.retention_days` дней."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.db import get_db
from app.models import Archive, User
from app.services import audit, infra
from app.services.settings_registry import get_setting
from app.web import json_download, render, require

router = APIRouter()


def _archive(db: Session, archive_id: int) -> Archive:
    archive = db.get(Archive, archive_id)
    if not archive:
        raise HTTPException(404, "Записи в архиве нет (возможно, истёк срок хранения)")
    return archive


@router.get("/archive")
def archive_page(request: Request, user: User = Depends(require("servers.view")), db: Session = Depends(get_db)):
    items = db.scalars(select(Archive).options(defer(Archive.data)).order_by(Archive.deleted_at.desc())).all()
    days = get_setting(db, "history.retention_days")
    return render(request, "archive.html", {"title": "Архив", "items": items, "days": days,
                                            "expires": {a.id: a.deleted_at + infra.retention(db) for a in items}},
                  user, db)


@router.get("/archive/{archive_id}/download")
def archive_download(archive_id: int, request: Request, user: User = Depends(require("servers.view")),
                     db: Session = Depends(get_db)):
    archive = _archive(db, archive_id)
    audit.log_event(db, "archive_download", request=request, username=user.username,
                    detail=f"архив «{archive.name}» ({archive.kind})")
    return json_download(infra.unpack(archive), f"archive-{archive.kind}-{archive.name}")


@router.post("/archive/{archive_id}/delete")
def archive_delete(archive_id: int, request: Request, user: User = Depends(require("servers.edit")),
                   db: Session = Depends(get_db)):
    archive = _archive(db, archive_id)
    name = archive.name
    db.delete(archive)
    db.commit()
    audit.log_event(db, "delete", request=request, username=user.username, detail=f"запись архива «{name}»")
    return RedirectResponse("/archive", 303)
