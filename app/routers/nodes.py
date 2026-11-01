"""Раздел «Узлы»: агенты homelab-agent. Права — те же, что у «Серверов» (`servers.view` / `servers.edit`)."""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.db import SessionLocal, get_db
from app.models import Device, Node, NodeAction, NodeEvent, Server, User
from app.routers import servers as servers_router
from app.routers import vpn as vpn_router
from urllib.parse import quote

from app.services import agent_remote, agent_tunnel, agent_updates, agents, audit, device_actions, infra
from app.utils import utcnow
from app.web import json_download, render, require

router = APIRouter()

# Виды датчиков, которые показываем графиками на странице узла
CHART_KINDS = ("temperature", "load", "memory", "disk", "frequency", "fan", "power", "latency")


def _node(db: Session, node_id: int) -> Node:
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(404, "Узел не найден")
    return node


def summary(db: Session, node: Node) -> dict:
    """Публичное представление узла (без хеша токена)."""
    values = node.values
    metrics = agents.server_metrics(values, node.sensors)
    temps = [values[s["id"]] for s in node.sensors if s["kind"] == "temperature" and s["id"] in values]
    root = next((d for d in metrics.disks if d["mount"] == "/"), metrics.disks[0] if metrics.disks else None)
    return {
        "id": node.id, "name": node.name, "online": agents.is_online(db, node), "version": node.version,
        "ips": node.ips, "remote_ip": node.remote_ip, "info": node.info,
        "last_seen": node.last_seen.isoformat() + "Z" if node.last_seen else None,
        "server": {"id": node.server.id, "name": node.server.name} if node.server else None,
        "cpu": values.get("cpu.percent"), "load1": values.get("cpu.load1"),
        "temp": max(temps) if temps else None,
        "ram": {"total": metrics.ram_total, "used": metrics.ram_used} if metrics.ram_total else None,
        "root_disk": root, "disks": metrics.disks, "uptime": values.get("uptime.seconds"),
        "actions": node.actions, "sensor_count": len(node.sensors),
    }


def _sensor_groups(node: Node) -> list[tuple[str, list[dict]]]:
    values, groups = node.values, {}
    for s in node.sensors:
        groups.setdefault(s["kind"], []).append({**s, "value": values.get(s["id"])})
    return sorted(groups.items())


# ---------- Страницы ----------

@router.get("/nodes")
def nodes_page(request: Request, user: User = Depends(require("servers.view")), db: Session = Depends(get_db)):
    nodes = [summary(db, n) for n in db.scalars(select(Node).order_by(Node.name))]
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    return render(request, "nodes/list.html", {"title": "Узлы", "nodes": nodes, "servers": servers,
                                                "flash": request.query_params.get("flash", "")[:300]}, user, db)


@router.get("/nodes/{node_id}")
def node_page(node_id: int, request: Request, user: User = Depends(require("servers.view")),
              db: Session = Depends(get_db)):
    node = _node(db, node_id)
    actions = db.scalars(select(NodeAction).where(NodeAction.node_id == node.id)
                         .order_by(NodeAction.id.desc()).limit(20)).all()
    events = db.scalars(select(NodeEvent).where(NodeEvent.node_id == node.id)
                        .order_by(NodeEvent.ts.desc()).limit(20)).all()
    servers = db.scalars(select(Server).order_by(Server.name)).all()
    ctx = {
        "title": node.name, "n": summary(db, node), "groups": _sensor_groups(node), "actions_log": actions,
        "events": events, "servers": servers, "chart_kinds": CHART_KINDS,
        "delete_confirm": infra.what_goes(db, node.server) if node.server
        else f"Удалить узел «{node.name}»? Токен перестанет действовать, история попадёт в архив.",
    }
    # Настройки узла (общий фрагмент с /servers/{id}/settings) — тем, кто может править, и если у узла есть сервер
    if node.server and user.can("servers.edit"):
        ctx.update(servers_router.settings_context(db, user, node.server, f"/nodes/{node.id}#settings",
                                                   f"/nodes/{node.id}/delete"))
    return render(request, "nodes/detail.html", ctx, user, db)


# ---------- API (только чтение) ----------

@router.get("/api/nodes")
def api_nodes(user: User = Depends(require("servers.view")), db: Session = Depends(get_db)):
    return [summary(db, n) for n in db.scalars(select(Node).order_by(Node.name))]


@router.get("/api/nodes/{node_id}/history")
def api_node_history(node_id: int, user: User = Depends(require("servers.view")), db: Session = Depends(get_db)):
    return agents.history(db, _node(db, node_id), set(CHART_KINDS))


# ---------- Изменение ----------

def _token_page(request: Request, user: User, db: Session, node: Node, token: str):
    """Токен показывается один раз — на этой странице; в БД остаётся только хеш."""
    response = render(request, "nodes/token.html", {
        "title": f"Токен узла {node.name}", "node": node, "token": token,
        "ws_url": str(request.base_url).replace("http", "ws", 1) + "agent/ws"}, user, db)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/nodes")
async def node_create(request: Request, user: User = Depends(require("servers.edit")), db: Session = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not agents.valid_name(name):
        raise HTTPException(400, "Имя узла: латиница, цифры, точка, дефис, подчёркивание, до 64 символов")
    if db.scalar(select(Node).where(Node.name == name)):
        raise HTTPException(400, "Узел с таким именем уже есть")
    node = Node(name=name)
    token = agents.issue_token(node)
    _link_server(db, node, form, user)
    db.add(node)
    db.commit()
    audit.log_event(db, "node_token", request=request, username=user.username, detail=f"узел «{node.name}» создан")
    return _token_page(request, user, db, node, token)


def _link_server(db: Session, node: Node, form, user: User) -> None:
    """Привязка к серверу: существующий, новый (сбор «agent») или никакой."""
    raw = str(form.get("server_id", ""))
    if raw == "new":
        host = str(form.get("host", "")).strip()[:255] or node.name
        server = Server(name=node.name, host=host, collector="agent", enabled=True)
        db.add(server)
        node.server = server
        if form.get("on_map") == "on" and user.can("topology.edit"):
            db.add(Device(name=node.name, type="server", ip=host[:64], server=server))
    elif raw.isdigit():
        server = db.get(Server, int(raw))
        if not server:
            raise HTTPException(400, "Сервер не найден")
        node.server = server
        if form.get("use_agent") == "on":
            server.collector = "agent"
    else:
        node.server = None


@router.post("/nodes/{node_id}")
async def node_update(node_id: int, request: Request, user: User = Depends(require("servers.edit")),
                      db: Session = Depends(get_db)):
    node = _node(db, node_id)
    _link_server(db, node, await request.form(), user)
    db.commit()
    return RedirectResponse(f"/nodes/{node.id}", 303)


@router.post("/nodes/{node_id}/server")
async def node_create_server(node_id: int, request: Request, user: User = Depends(require("servers.edit")),
                             db: Session = Depends(get_db)):
    """«Создать карточку сервера»: сервер со сбором agent, имя узла; адрес — из формы, иначе последний IP агента."""
    node = _node(db, node_id)
    if node.server:
        raise HTTPException(400, "У узла уже есть сервер")
    form = await request.form()
    host = str(form.get("host", "")).strip()[:255] or (node.ips[0] if node.ips else "") or node.remote_ip or ""
    if not host:
        raise HTTPException(400, "Укажите адрес сервера: агент ещё не присылал свой IP")
    server = Server(name=node.name, host=host, collector="agent", enabled=True)
    node.server = server
    db.add(server)
    db.commit()
    audit.log_event(db, "node_server", request=request, username=user.username,
                    detail=f"узел «{node.name}»: создана карточка сервера")
    return RedirectResponse(f"/nodes/{node.id}#settings", 303)


@router.post("/nodes/{node_id}/token")
async def node_token(node_id: int, request: Request, user: User = Depends(require("servers.edit")),
                     db: Session = Depends(get_db)):
    node = _node(db, node_id)
    token = agents.issue_token(node)
    db.commit()
    audit.log_event(db, "node_token", request=request, username=user.username, detail=f"новый токен узла «{node.name}»")
    # Старое соединение с прежним токеном закрываем — агент переподключится уже с новым
    await agents.close_nodes([node.id])
    return _token_page(request, user, db, node, token)


@router.post("/nodes/{node_id}/delete")
async def node_delete(node_id: int, request: Request, user: User = Depends(require("servers.edit")),
                      db: Session = Depends(get_db)):
    node = _node(db, node_id)
    what = f"узел «{node.name}»" + (f" и сервер «{node.server.name}» везде" if node.server else "")
    note = await agent_remote.before_delete(db, request, node, user)
    await agents.close_nodes(infra.delete_node(db, node, user))
    audit.log_event(db, "delete", request=request, username=user.username, detail=what + " (в архив)")
    return RedirectResponse("/nodes" + (f"?flash={quote(note[:250])}" if note else ""), 303)


# ---------- Настройки агента ----------

def _config_page(request: Request, user: User, db: Session, node: Node, cfg: dict | None = None,
                 error: str = "", result: str = "", status: int = 200):
    return render(request, "nodes/agent_config.html", {
        "title": f"Настройки агента · {node.name}", "n": node, "cfg": cfg, "error": error, "result": result,
    }, user, db, status)


@router.post("/nodes/{node_id}/config/get")
async def node_config_get(node_id: int, request: Request, user: User = Depends(require("servers.edit")),
                          db: Session = Depends(get_db)):
    node = _node(db, node_id)
    try:
        cfg = await agent_remote.get_config(db, node)
    except agent_remote.RemoteError as e:
        return _config_page(request, user, db, node, error=str(e), status=409)
    return _config_page(request, user, db, node, cfg)


@router.post("/nodes/{node_id}/config/set")
async def node_config_set(node_id: int, request: Request, user: User = Depends(require("servers.edit")),
                          db: Session = Depends(get_db)):
    node = _node(db, node_id)
    form = await request.form()
    try:
        sections = agent_remote.parse_sections(form)
        ok, detail = await agent_remote.set_config(db, node, sections)
    except agent_remote.RemoteError as e:
        return _config_page(request, user, db, node, error=str(e), status=409)
    audit.log_event(db, "agent_config", ok=ok, request=request, username=user.username,
                    detail=f"{node.name}: {', '.join(sections)} — {detail or ('сохранено' if ok else 'отказ')}")
    if not ok:
        return _config_page(request, user, db, node, error=f"Агент отказал: {detail or 'ошибка'}", status=409)
    return _config_page(request, user, db, node, result="Агент принял настройки и перезапускается" +
                        (f": {detail}" if detail else ""))


@router.get("/nodes/{node_id}/history.json")
def node_history_download(node_id: int, request: Request, user: User = Depends(require("servers.view")),
                          db: Session = Depends(get_db)):
    node = _node(db, node_id)
    since = utcnow() - infra.retention(db)
    payload = {"kind": "node", "exported_at": utcnow().isoformat() + "Z", "nodes": [infra.node_snapshot(db, node, since)]}
    audit.log_event(db, "archive_download", request=request, username=user.username, detail=f"история узла «{node.name}»")
    return json_download(payload, f"history-{node.name}")


@router.post("/nodes/{node_id}/actions/{name}")
async def node_action(node_id: int, name: str, user: User = Depends(require("servers.edit")),
                      db: Session = Depends(get_db)):
    result = await agents.request_action(db, _node(db, node_id), name, user)
    if isinstance(result, str):
        raise HTTPException(409, result)
    return RedirectResponse(f"/nodes/{node_id}#actions", 303)


# ---------- WebSocket агентов ----------

@router.websocket("/agent/ws")
async def agent_ws(ws: WebSocket):
    await ws.accept()
    remote_ip = ws.client.host if ws.client else ""
    try:
        first = await asyncio.wait_for(ws.receive_text(), 15)
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        await _close(ws)
        return
    hello = agents.parse_hello(_json(first))
    with SessionLocal() as db:
        node = hello if isinstance(hello, str) else agents.handle_hello(db, hello, remote_ip)
        claimed = hello["node"] if isinstance(hello, dict) else ""
        audit.log_event(db, "agent_auth", ok=not isinstance(node, str), ip=remote_ip,
                        username=claimed, detail="подключён" if not isinstance(node, str) else node)
        if isinstance(node, str) and isinstance(hello, dict) and (tomb := agent_remote.find_tombstone(db, hello["token"])):
            # Узел удалён в панели, пока агент был офлайн: просим его удалить себя
            audit.log_event(db, "agent_uninstall", ip=remote_ip, username=tomb.name,
                            detail="удалённый узел вернулся на связь — отправлена команда uninstall")
            await _tombstone_uninstall(ws, db, tomb)
            return
        if isinstance(node, str):
            await asyncio.sleep(1)  # неверный токен: замедляем перебор
            await ws.send_text(json.dumps({"type": "error", "message": node}))
            await _close(ws, 1008)
            return
        node_id = node.id
        await ws.send_text(json.dumps({"type": "welcome", "interval_ms": agents.interval(db) * 1000,
                                       "v": agents.PROTOCOL_VERSION, "panel_version": __version__}))
    old = agents.hub.register(node_id, ws)
    if old:
        agent_tunnel.drop_node(node_id)  # туннели шли по вытесненному соединению
        await _close(old, 1000)  # второй агент с тем же именем вытесняет первого
    with SessionLocal() as db:
        node = db.get(Node, node_id)
        if node:
            await vpn_router.on_hello(db, node)  # ждущее назначение VPN — дослать
            agent_updates.on_hello(db, node)  # вернулся с новой версией — обновление подтверждено
    try:
        while True:
            text = await ws.receive_text()
            if len(text) > agents.MAX_MESSAGE:
                continue
            data = _json(text)
            kind = data.get("type") if isinstance(data, dict) else None
            if isinstance(kind, str) and kind.startswith("tunnel_"):  # поток байт: без обращения к БД на каждый кусок
                agent_tunnel.on_message(node_id, data)
                continue
            with SessionLocal() as db:
                node = db.get(Node, node_id)
                if not node:  # узел удалили в панели
                    break
                if kind == "sample":
                    agents.handle_sample(db, node, data)
                elif kind == "action_result":
                    agents.handle_action_result(db, node, data)
                elif kind == "event":
                    agents.handle_event(db, node, data)
                elif kind == "vpn_result":
                    vpn_router.on_result(db, node, data)
                elif kind == "update_result":
                    agent_updates.on_result(db, node, data)
                elif kind == "wol_result":
                    device_actions.on_wol_result(node, data)
                elif kind in ("config", "config_result", "uninstall_result"):
                    agent_remote.on_message(node, data)
                # остальное игнорируем (совместимость с будущими версиями протокола)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if agents.hub.unregister(node_id, ws):
            agent_tunnel.drop_node(node_id)
            with SessionLocal() as db:
                agents.disconnect(db, node_id)
        await _close(ws)


async def _tombstone_uninstall(ws: WebSocket, db: Session, tomb) -> None:
    """Команда uninstall агенту удалённого узла; при ok надгробие больше не нужно."""
    rid = f"t-{tomb.id}"
    try:
        await ws.send_text(json.dumps({"type": "uninstall", "id": rid}))
        data = _json(await asyncio.wait_for(ws.receive_text(), agent_remote.UNINSTALL_TIMEOUT))
        if isinstance(data, dict) and data.get("type") == "uninstall_result" and data.get("id") == rid:
            if data.get("ok") is True:
                db.delete(tomb)
                db.commit()
            audit.log_event(db, "agent_uninstall", ok=data.get("ok") is True, username=tomb.name,
                            detail=str(data.get("detail", ""))[:200] or "ответ агента")
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        pass
    await _close(ws)


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


async def _close(ws: WebSocket, code: int = 1000) -> None:
    try:
        await ws.close(code=code)
    except RuntimeError:
        pass  # уже закрыт
