"""Сервисы: общий список сервисов всех узлов и страница сервиса.

Проверка доступности — как раньше (monitoring.check_service); здесь только просмотр и правка записей.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Server, Service, User
from app.services import audit
from app.web import render, require

router = APIRouter()

# Вид сервиса → (название, иконка). Новый вид — одна строка; иконку добавить в tools/build_icons.py ICONS
SERVICE_KINDS = {
    "generic": ("Сервис", "boxes"),
    "filebrowser": ("Файловый менеджер", "folder"),
}
CHECK_TYPES = ("tcp", "http")


def _get_service(db: Session, service_id: int) -> Service:
    service = db.get(Service, service_id)
    if not service:
        raise HTTPException(404, "Сервис не найден")
    return service


def _parse(db: Session, form) -> dict:
    """Проверяет форму; возвращает поля сервиса. Ошибка → 400."""
    name = str(form.get("name", "")).strip()[:128]
    target = str(form.get("target", "")).strip()[:500]
    check_type = str(form.get("check_type", "tcp"))
    kind = str(form.get("kind", "generic"))
    web_url = str(form.get("web_url", "")).strip()
    description = str(form.get("description", "")).strip()[:2000]
    try:
        server = db.get(Server, int(form.get("server_id", "")))
    except (TypeError, ValueError):
        server = None
    if not server:
        raise HTTPException(400, "Выберите узел")
    if not name or not target or check_type not in CHECK_TYPES:
        raise HTTPException(400, "Укажите имя, тип проверки и адрес")
    if kind not in SERVICE_KINDS:
        raise HTTPException(400, "Неизвестный вид сервиса")
    if check_type == "http" and not target.startswith(("http://", "https://")):
        raise HTTPException(400, "Для http нужен URL с http:// или https://")
    if check_type == "tcp" and not target.rpartition(":")[2].isdigit():
        raise HTTPException(400, "Для tcp нужен адрес вида host:port")
    # Только http(s): иначе javascript:/data: в кнопке «Открыть» — XSS
    if web_url and (len(web_url) > 500 or not web_url.startswith(("http://", "https://"))):
        raise HTTPException(400, "Веб-адрес должен начинаться с http:// или https://")
    return {"name": name, "server_id": server.id, "check_type": check_type, "target": target,
            "kind": kind, "web_url": web_url, "description": description}


def _form_ctx(db: Session) -> dict:
    return {"servers": db.scalars(select(Server).order_by(Server.name)).all(), "kinds": SERVICE_KINDS}


@router.get("/services")
def services_page(request: Request, q: str = "", user: User = Depends(require("services.view")),
                  db: Session = Depends(get_db)):
    q = q.strip()[:100]
    query = select(Service).join(Server).order_by(Service.name, Server.name)
    if q:
        query = query.where(Service.name.icontains(q, autoescape=True))
    return render(request, "services/list.html",
                  {"title": "Сервисы", "services": db.scalars(query).all(), "q": q, **_form_ctx(db)}, user, db)


@router.get("/services/{service_id}")
def service_page(service_id: int, request: Request, user: User = Depends(require("services.view")),
                 db: Session = Depends(get_db)):
    service = _get_service(db, service_id)
    return render(request, "services/detail.html", {"title": service.name, "sv": service, **_form_ctx(db)}, user, db)


@router.post("/services")
async def service_create(request: Request, user: User = Depends(require("services.edit")),
                         db: Session = Depends(get_db)):
    service = Service(**_parse(db, await request.form()))
    db.add(service)
    db.commit()
    return RedirectResponse(f"/services/{service.id}", 303)


@router.post("/services/{service_id}")
async def service_update(service_id: int, request: Request, user: User = Depends(require("services.edit")),
                         db: Session = Depends(get_db)):
    service = _get_service(db, service_id)
    for key, value in _parse(db, await request.form()).items():
        setattr(service, key, value)
    # Адрес или тип проверки могли измениться — старый результат уже недействителен
    service.ok, service.last_checked, service.last_error = None, None, ""
    db.commit()
    return RedirectResponse(f"/services/{service_id}", 303)


@router.post("/services/{service_id}/remove")
async def service_remove(service_id: int, request: Request, user: User = Depends(require("services.edit")),
                         db: Session = Depends(get_db)):
    service = _get_service(db, service_id)
    name = service.name
    db.delete(service)
    db.commit()
    audit.log_event(db, "delete", request=request, username=user.username, detail=f"сервис «{name}»")
    return RedirectResponse("/services", 303)
