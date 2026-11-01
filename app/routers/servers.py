from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Device, Link, Node, Server, Service, User
from app.routers.auth import safe_next
from app.routers.home import kpi, widget
from app.routers.settings import SECTION_EXTRA
from app.services import agent_remote, agents, audit, infra, monitoring
from app.services.security import encrypt
from app.utils import utcnow
from app.web import json_download, render, require

router = APIRouter()


def _get_server(db: Session, server_id: int) -> Server:
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404, "Сервер не найден")
    return server


def _apply_form(server: Server, form) -> None:
    """Заполняет сервер из формы с проверкой. Пустой путь к ключу — оставить прежний."""
    name, host = str(form.get("name", "")).strip(), str(form.get("host", "")).strip()
    collector = form.get("collector", "local")
    try:
        port = int(form.get("ssh_port") or 22)
    except ValueError:
        port = 0
    if not name or not host or len(name) > 128 or len(host) > 255:
        raise HTTPException(400, "Укажите имя и адрес")
    if not 1 <= port <= 65535 or collector not in ("local", "ssh", "agent"):
        raise HTTPException(400, "Неверный порт или способ сбора")
    server.name, server.host, server.ssh_port, server.collector = name, host, port, collector
    server.ssh_user = str(form.get("ssh_user", "")).strip()[:64]
    server.enabled = form.get("enabled") == "on"
    key_path = str(form.get("ssh_key_path", "")).strip()
    if key_path:
        server.ssh_key_enc = encrypt(key_path)
    if form.get("clear_key") == "on":
        server.ssh_key_enc = ""


def _back(form, default: str) -> RedirectResponse:
    return RedirectResponse(safe_next(str(form.get("back", default))), 303)


# ---------- Страницы ----------

@router.get("/servers")
def servers_page(request: Request, user: User = Depends(require("servers.view")), db: Session = Depends(get_db)):
    servers = [monitoring.server_summary(db, s) for s in db.scalars(select(Server).order_by(Server.name))]
    return render(request, "servers/list.html", {"title": "Серверы", "servers": servers}, user, db)


@router.get("/servers/{server_id}")
def server_page(server_id: int, request: Request, user: User = Depends(require("servers.view")),
                db: Session = Depends(get_db)):
    server = _get_server(db, server_id)
    device = db.scalar(select(Device).where(Device.server_id == server.id)) if user.can("topology.view") else None
    return render(request, "servers/detail.html",
                  {"title": server.name, "s": monitoring.server_summary(db, server), "device": device,
                   "delete_confirm": infra.what_goes(db, server)}, user, db)


# ---------- API (только чтение) ----------

@router.get("/api/servers")
def api_servers(user: User = Depends(require("servers.view")), db: Session = Depends(get_db)):
    return [monitoring.server_summary(db, s) for s in db.scalars(select(Server).order_by(Server.name))]


@router.get("/api/servers/{server_id}/history")
def api_history(server_id: int, user: User = Depends(require("servers.view")), db: Session = Depends(get_db)):
    _get_server(db, server_id)
    return monitoring.history(db, server_id)


# ---------- Изменение ----------

@router.post("/servers")
async def server_create(request: Request, user: User = Depends(require("servers.edit")),
                        db: Session = Depends(get_db)):
    form = await request.form()
    server = Server()
    _apply_form(server, form)
    db.add(server)
    if form.get("on_map") == "on" and user.can("topology.edit"):
        _place_on_map(db, server, str(form.get("uplink_id", "")))
    db.commit()
    return _back(form, "/settings/servers")


def _place_on_map(db: Session, server: Server, uplink_raw: str) -> None:
    """Добавляет сервер на топологию узлом типа server и, если указано, связывает с устройством-аплинком."""
    device = Device(name=server.name, type="server", ip=server.host[:64], server=server)
    db.add(device)
    uplink = db.get(Device, int(uplink_raw)) if uplink_raw.isdigit() else None
    if uplink:
        db.add(Link(a=uplink, b=device))


@router.post("/servers/{server_id}")
async def server_update(server_id: int, request: Request, user: User = Depends(require("servers.edit")),
                        db: Session = Depends(get_db)):
    server = _get_server(db, server_id)
    form = await request.form()
    _apply_form(server, form)
    db.commit()
    return _back(form, "/settings/servers")


@router.post("/servers/{server_id}/delete")
async def server_delete(server_id: int, request: Request, user: User = Depends(require("servers.edit")),
                        db: Session = Depends(get_db)):
    server = _get_server(db, server_id)
    name = server.name
    await agent_remote.before_delete(db, request, server, user)
    await agents.close_nodes(infra.delete_server(db, server, user))
    audit.log_event(db, "delete", request=request, username=user.username, detail=f"сервер «{name}» (везде, в архив)")
    back = str((await request.form()).get("back", ""))
    # Со страницы удалённого сервера возвращаться некуда
    return RedirectResponse(safe_next(back) if back and back != f"/servers/{server_id}" else "/servers", 303)


@router.post("/servers/{server_id}/check")
async def server_check(server_id: int, request: Request, user: User = Depends(require("servers.edit")),
                       db: Session = Depends(get_db)):
    server = _get_server(db, server_id)
    await monitoring.check_server(db, server)
    for service in server.services:
        await monitoring.check_service(db, service)
    return _back(await request.form(), f"/servers/{server_id}")


@router.post("/servers/{server_id}/services")
async def service_create(server_id: int, request: Request, user: User = Depends(require("servers.edit")),
                         db: Session = Depends(get_db)):
    server = _get_server(db, server_id)
    form = await request.form()
    name, target = str(form.get("name", "")).strip()[:128], str(form.get("target", "")).strip()[:500]
    check_type = form.get("check_type", "tcp")
    if not name or not target or check_type not in ("tcp", "http"):
        raise HTTPException(400, "Укажите имя, тип и адрес сервиса")
    if check_type == "http" and not target.startswith(("http://", "https://")):
        raise HTTPException(400, "Для http нужен URL с http:// или https://")
    if check_type == "tcp" and not target.rpartition(":")[2].isdigit():
        raise HTTPException(400, "Для tcp нужен адрес вида host:port")
    db.add(Service(server_id=server.id, name=name, check_type=check_type, target=target))
    db.commit()
    return _back(form, f"/servers/{server_id}")


@router.post("/services/{service_id}/delete")
async def service_delete(service_id: int, request: Request, user: User = Depends(require("servers.edit")),
                         db: Session = Depends(get_db)):
    service = db.get(Service, service_id)
    if not service:
        raise HTTPException(404, "Сервис не найден")
    server_id, name = service.server_id, service.name
    db.delete(service)
    db.commit()
    audit.log_event(db, "delete", request=request, username=user.username, detail=f"сервис «{name}»")
    return _back(await request.form(), f"/servers/{server_id}")


@router.get("/servers/{server_id}/history.json")
def server_history_download(server_id: int, request: Request, user: User = Depends(require("servers.view")),
                            db: Session = Depends(get_db)):
    """История сервера за весь срок хранения (и узла с агентом, если привязан) — файлом JSON."""
    server = _get_server(db, server_id)
    since = utcnow() - infra.retention(db)
    nodes = db.scalars(select(Node).where(Node.server_id == server.id)).all()
    payload = {"kind": "server", "exported_at": utcnow().isoformat() + "Z", "server": infra.server_snapshot(db, server, since),
               "nodes": [infra.node_snapshot(db, n, since) for n in nodes]}
    audit.log_event(db, "archive_download", request=request, username=user.username, detail=f"история сервера «{server.name}»")
    return json_download(payload, f"history-{server.name}")


def _all_summaries(db: Session, user: User | None = None) -> dict:
    data = {"servers": [{**monitoring.server_summary(db, s), "delete_confirm": infra.what_goes(db, s)}
                        for s in db.scalars(select(Server).order_by(Server.name))]}
    # Для формы «Новый сервер»: к какому устройству топологии он подключён
    if user and user.can("topology.edit"):
        data["devices"] = db.scalars(select(Device).order_by(Device.name)).all()
    return data


# Список серверов в Настройки → Серверы и сервисы и виджет на главной
SECTION_EXTRA["servers"] = _all_summaries
widget("servers", "servers.view")(_all_summaries)


@kpi("servers.view")
def _servers_kpi(db: Session, user: User) -> dict:
    servers = db.scalars(select(Server).where(Server.enabled.is_(True))).all()
    online = sum(1 for s in servers if s.online)
    return {"icon": "server", "label": "Серверов онлайн", "value": f"{online} / {len(servers)}", "link": "/servers"}
