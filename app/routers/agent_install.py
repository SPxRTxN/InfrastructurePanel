"""Автоустановка агента: адреса с одноразовым кодом (без сессии) и блок «Агент» в настройках узла.

Права: показать команду — `servers.edit`; установка по SSH — `servers.edit` + `servers.console` (панель входит на
машину сохранённым входом); галочка VPN учитывается только при `settings.manage`.
"""
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AgentRelease, Node, Server, User
from app.routers.console import _cred
from app.services import agent_install as svc
from app.services import agents, audit, ssh_access
from app.web import render, require

router = APIRouter()

_NOT_FOUND = HTTPException(404, "Не найдено")


def _by_code(request: Request, db: Session, code: str):
    inst = svc.by_code(db, code)
    if inst is None:
        # Код в журнал не пишем — только факт
        audit.log_event(db, "agent_install", ok=False, request=request, detail="неизвестный или истёкший код установки")
        raise _NOT_FOUND
    return inst


# ---------- Без сессии: авторизация — код в пути ----------

@router.get("/agent/install/{code}")
def install_script(code: str, request: Request, db: Session = Depends(get_db)):
    inst = _by_code(request, db, code)
    node, release = db.get(Node, inst.node_id), svc.latest_release(db)
    if node is None or release is None or svc.release_file(release) is None:
        raise HTTPException(503, "На панели нет версии агента")
    script = svc.render_script(str(request.base_url), code, node, release, inst.vpn, inst.own_python)
    audit.log_event(db, "agent_install", request=request, detail=f"выдан скрипт установки для «{node.name}»")
    return PlainTextResponse(script, media_type="text/x-shellscript")


@router.get("/agent/install/{code}/windows")
def install_script_windows(code: str, request: Request, db: Session = Depends(get_db)):
    inst = _by_code(request, db, code)
    node, release = db.get(Node, inst.node_id), svc.latest_release(db)
    if node is None or release is None or svc.release_file(release) is None:
        raise HTTPException(503, "На панели нет версии агента")
    script = svc.render_windows(str(request.base_url), code, node, release)
    audit.log_event(db, "agent_install", request=request, detail=f"выдан скрипт установки (Windows) для «{node.name}»")
    # charset обязателен: Windows PowerShell 5.1 без него читает ответ как Latin-1 — кириллица ломается
    return PlainTextResponse(script, media_type="text/plain; charset=utf-8")


@router.get("/agent/install/{code}/bundle")
def install_bundle(code: str, request: Request, db: Session = Depends(get_db)):
    _by_code(request, db, code)
    # Версия — та, чей SHA вшит в скрипт (?v=): новая версия, загруженная между запросами, не ломает установку
    wanted = request.query_params.get("v", "")
    release = (db.scalar(select(AgentRelease).where(AgentRelease.version == wanted)) if wanted else None) \
        or svc.latest_release(db)
    path = svc.release_file(release) if release else None
    if not path:
        raise HTTPException(503, "На панели нет версии агента")
    return FileResponse(path, media_type="application/gzip", filename=f"homelab-agent-{release.version}.tar.gz")


@router.post("/agent/install/{code}/token")
def install_token(code: str, request: Request, db: Session = Depends(get_db)):
    inst = _by_code(request, db, code)
    node = db.get(Node, inst.node_id)
    token = svc.issue_token(db, inst)
    if token is None:
        audit.log_event(db, "agent_install", ok=False, request=request, detail="повторный запрос токена по тому же коду")
        raise HTTPException(409, "Токен по этой ссылке уже выдан")
    audit.log_event(db, "agent_install", request=request, detail=f"токен узла «{node.name}» выдан установщику")
    return JSONResponse({"token": token}, headers={"Cache-Control": "no-store"})


@router.post("/agent/install/{code}/report")
async def install_report(code: str, request: Request, db: Session = Depends(get_db)):
    inst = _by_code(request, db, code)
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > svc.MAX_REPORT:
        raise HTTPException(413, "Отчёт слишком большой")
    body = await request.body()
    if len(body) > svc.MAX_REPORT:
        raise HTTPException(413, "Отчёт слишком большой")
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError
        svc.add_report(db, inst, data)
    except ValueError:
        raise HTTPException(400, "Неверный отчёт") from None
    if data.get("step") == "done":
        node = db.get(Node, inst.node_id)
        audit.log_event(db, "agent_install", ok=inst.status == "ok", request=request,
                        detail=f"установка агента на «{node.name if node else '?'}»: "
                               f"{'готово' if inst.status == 'ok' else 'ошибка'}")
    return {"ok": True}


# ---------- Блок «Агент» в настройках узла ----------

def _server(db: Session, server_id: int) -> Server:
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404, "Сервер не найден")
    return server


def _node_of(db: Session, server: Server) -> Node | None:
    return db.scalar(select(Node).where(Node.server_id == server.id).order_by(Node.id))


def _ensure_node(db: Session, server: Server) -> Node:
    """Узла у сервера нет — создаём (имя = имя сервера, приведённое к допустимому). Токен тут же заменит установка."""
    node = _node_of(db, server)
    if node:
        return node
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", server.name).strip("-._")[:56] or f"server-{server.id}"
    name, n = base, 1
    while db.scalar(select(Node).where(Node.name == name)):
        n += 1
        name = f"{base}-{n}"
    node = Node(name=name, server=server)
    agents.issue_token(node)  # токен нужен только как заглушка: настоящий выдаётся установщику один раз
    db.add(node)
    db.commit()
    return node


def _fragment(request: Request, db: Session, user: User, server: Server, command: str = "", part: str = "",
              command_win: str = ""):
    node = _node_of(db, server)
    can_ssh = user.can("servers.console")
    creds = [{"id": c.id, "label": c.label or "OS", "address": ssh_access.ssh_command(c.username, c.host or server.host, c.port)}
             for c in server.credentials if c.kind == "os"] if can_ssh else []
    agent = {"node": node, "online": bool(node and agents.is_online(db, node)), "command": command,
             "command_win": command_win,
             "install": svc.view(svc.last_for(db, node)), "can_ssh": can_ssh, "creds": creds,
             "can_vpn": user.can("settings.manage")}
    response = render(request, "servers/_agent_install.html", {"srv": server, "agent": agent, "part": part}, user, db)
    response.headers["Cache-Control"] = "no-store"  # в ответе может быть одноразовая команда
    return response


@router.get("/servers/{server_id}/agent-install")
def fragment(server_id: int, request: Request, user: User = Depends(require("servers.edit")),
             db: Session = Depends(get_db)):
    return _fragment(request, db, user, _server(db, server_id))


@router.get("/servers/{server_id}/agent-install/progress")
def progress(server_id: int, request: Request, user: User = Depends(require("servers.edit")),
             db: Session = Depends(get_db)):
    return _fragment(request, db, user, _server(db, server_id), part="progress")


async def _own_python(request: Request) -> bool:
    return (await request.form()).get("own_python") in ("on", "1", "true")


async def _vpn_flag(request: Request, user: User) -> bool:
    return (await request.form()).get("vpn") in ("on", "1", "true") and user.can("settings.manage")


@router.post("/servers/{server_id}/agent-install")
async def issue_command(server_id: int, request: Request, user: User = Depends(require("servers.edit")),
                        db: Session = Depends(get_db)):
    server = _server(db, server_id)
    vpn = await _vpn_flag(request, user)
    node = _ensure_node(db, server)
    _, code = svc.create(db, node, user.id, vpn, "command", await _own_python(request))
    audit.log_event(db, "agent_install", request=request, username=user.username,
                    detail=f"команда установки агента для «{node.name}»{', с VPN' if vpn else ''}")
    return _fragment(request, db, user, server, command=svc.command(str(request.base_url), code),
                     command_win=svc.windows_command(str(request.base_url), code))


@router.post("/servers/{server_id}/agent-install/ssh")
async def install_by_ssh(server_id: int, request: Request, user: User = Depends(require("servers.edit")),
                         db: Session = Depends(get_db)):
    if not user.can("servers.console"):
        raise HTTPException(403, "Недостаточно прав")
    server = _server(db, server_id)
    form = await request.form()
    cred = _cred(db, server, form.get("cred"))
    sudo_password = str(form.get("sudo_password", ""))[:256]
    vpn = await _vpn_flag(request, user)
    if cred is not None and cred.kind != "os":
        raise HTTPException(400, "Нужен вход ОС, не BMC")
    try:
        target = ssh_access.resolve(server, cred)
    except ssh_access.BadTarget as exc:
        raise HTTPException(400, str(exc)) from None
    start_ssh_install(request, db, user, server, target, sudo_password, vpn, await _own_python(request))
    return _fragment(request, db, user, server)


def start_ssh_install(request: Request, db: Session, user: User, server: Server, target: ssh_access.Target,
                      sudo_password: str, vpn: bool, own_python: bool) -> Node:
    """Фоновая установка по SSH (кнопка в настройках и «Новый сервер» с галочкой). Пароли не сохраняются."""
    node = _ensure_node(db, server)
    inst, code = svc.create(db, node, user.id, vpn, "ssh", own_python)
    svc.set_status(inst, "running")
    db.commit()
    audit.log_event(db, "agent_install", request=request, username=user.username,
                    detail=f"установка агента по SSH на «{node.name}», {target.label} {target.address}"
                           f"{', с VPN' if vpn else ''}")
    svc.spawn(inst.id, target, svc.install_url(str(request.base_url), code), sudo_password or target.password)
    return node
