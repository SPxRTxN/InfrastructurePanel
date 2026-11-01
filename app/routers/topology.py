from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Device, Link, Segment, Server, User
from app.models.topology import DEVICE_TYPES
from app.services import agent_remote, agents, audit, device_actions, infra
from app.services import ports as ports_svc
from app.web import render, require

router = APIRouter()
LINK_KINDS = ("wired", "wifi")


def _device(db: Session, device_id: int) -> Device:
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(404, "Устройство не найдено")
    return device


def _segment(db: Session, segment_id: int) -> Segment:
    segment = db.get(Segment, segment_id)
    if not segment:
        raise HTTPException(404, "Сегмент не найден")
    return segment


def device_status(device: Device) -> str:
    """Статус узла по связанному серверу: ok / bad / unknown; none — сервер не привязан."""
    if not device.server:
        return "none"
    if not device.server.enabled or device.server.online is None:
        return "unknown"
    return "ok" if device.server.online else "bad"


def _apply_device_form(db: Session, device: Device, form, user: User) -> None:
    name = str(form.get("name", "")).strip()
    dtype = form.get("type", "other")
    if not name or len(name) > 128 or dtype not in DEVICE_TYPES:
        raise HTTPException(400, "Укажите имя и тип устройства")
    server_raw = str(form.get("server_id", ""))
    server_id = int(server_raw) if server_raw.isdigit() else None
    if server_id and not db.get(Server, server_id):
        raise HTTPException(400, "Сервер не найден")
    device.name, device.type, device.server_id = name, dtype, server_id
    # Порты: ручной список главнее; если пуст — берём выбранный шаблон
    ports = ports_svc.parse_ports(str(form.get("ports", "")))
    template = ports_svc.TEMPLATES.get(str(form.get("ports_template", "")))
    if not ports and template:
        ports = template["ports"]
    device.ports = "\n".join(ports)
    seg_raw = str(form.get("segment_id", ""))
    device.segment_id = _segment(db, int(seg_raw)).id if seg_raw.isdigit() else None
    device.ip = str(form.get("ip", "")).strip()[:64]
    device.description = str(form.get("description", "")).strip()[:1000]
    try:
        device.mac = device_actions.normalize_mac(str(form.get("mac", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if form.get("make_server") == "on" and not server_id and user.can("servers.edit"):
        if not device.ip:
            raise HTTPException(400, "Чтобы добавить устройство в «Серверы», укажите IP")
        # Выключен до настройки ключа: SSH-проверка без ключа всё равно не пройдёт
        device.server = Server(name=name, host=device.ip, collector="ssh", enabled=False)
        db.add(device.server)


@router.get("/topology")
def topology_page(request: Request, user: User = Depends(require("topology.view")), db: Session = Depends(get_db)):
    devices = db.scalars(select(Device).order_by(Device.name)).all()
    servers = db.scalars(select(Server).order_by(Server.name)).all() if user.can("servers.view") else []
    links = db.scalars(select(Link)).all()
    segments = db.scalars(select(Segment).order_by(Segment.name)).all()
    return render(request, "topology.html", {"title": "Карта сети","devices": devices, "links": links,
                                             "servers": servers, "types": DEVICE_TYPES, "segments": segments,
                                             "port_templates": ports_svc.TEMPLATES}, user, db)


def _segment_json(s: Segment) -> dict:
    return {"id": s.id, "name": s.name, "color": s.color, "subnet": s.subnet, "description": s.description}


@router.get("/api/topology")
def api_topology(user: User = Depends(require("topology.view")), db: Session = Depends(get_db)):
    show_servers = user.can("servers.view")
    can_edit = user.can("topology.edit")
    nodes = []
    all_links = db.scalars(select(Link)).all()
    for d in db.scalars(select(Device)):
        used = ports_svc.used_ports(all_links, d.id)
        nodes.append({
            "id": d.id, "name": d.name, "type": d.type, "ip": d.ip, "description": d.description,
            "segment_id": d.segment_id,
            "ports": [{"name": p, "used": p in used} for p in ports_svc.parse_ports(d.ports)],
            "mac": d.mac if can_edit else "",
            "actions": device_actions.available(db, d, user),
            "x": d.x, "y": d.y,
            # Статус и имя сервера — только тем, кому видны серверы
            "status": device_status(d) if show_servers else "none",
            "server": {"id": d.server.id, "name": d.server.name} if show_servers and d.server else None,
        })
    edges = [{"id": lk.id, "source": lk.a_id, "target": lk.b_id, "a_port": lk.a_port, "b_port": lk.b_port,
              "label": lk.label, "kind": lk.kind} for lk in all_links]
    segments = [_segment_json(s) for s in db.scalars(select(Segment).order_by(Segment.name))]
    return {"nodes": nodes, "edges": edges, "segments": segments, "editable": can_edit}


class Position(BaseModel):
    id: int
    x: float = Field(ge=-1e6, le=1e6)
    y: float = Field(ge=-1e6, le=1e6)


class PositionsIn(BaseModel):
    positions: list[Position] = Field(max_length=1000)


@router.post("/api/topology/positions")
def save_positions(body: PositionsIn, user: User = Depends(require("topology.edit")), db: Session = Depends(get_db)):
    by_id = {d.id: d for d in db.scalars(select(Device).where(Device.id.in_([p.id for p in body.positions])))}
    for p in body.positions:
        if p.id in by_id:
            by_id[p.id].x, by_id[p.id].y = round(p.x, 1), round(p.y, 1)
    db.commit()
    return {"saved": len(by_id)}


@router.post("/topology/devices")
async def device_create(request: Request, user: User = Depends(require("topology.edit")),
                        db: Session = Depends(get_db)):
    device = Device()
    _apply_device_form(db, device, await request.form(), user)
    db.add(device)
    db.commit()
    return RedirectResponse("/topology", 303)


@router.post("/topology/devices/{device_id}")
async def device_update(device_id: int, request: Request, user: User = Depends(require("topology.edit")),
                        db: Session = Depends(get_db)):
    _apply_device_form(db, _device(db, device_id), await request.form(), user)
    db.commit()
    return RedirectResponse("/topology", 303)


@router.post("/topology/devices/{device_id}/actions/{action_id}")
async def device_action(device_id: int, action_id: str, request: Request,
                        user: User = Depends(require("topology.view")), db: Session = Depends(get_db)):
    """Действие над устройством (не ссылка). Право проверяет сам реестр: для WoL — topology.edit."""
    device = _device(db, device_id)
    form = await request.form()
    action = device_actions.ACTIONS.get(action_id)
    try:
        message = await device_actions.run(db, device, action_id, user, form)
        ok = True
    except device_actions.ActionError as e:
        if e.status in (403, 404, 400) or not action:
            raise HTTPException(e.status, e.message)
        message, ok = e.message, False
    audit.log_event(db, "device_action", ok=ok, request=request, username=user.username,
                    detail=f"{action.label}: «{device.name}» — {message}")
    if not ok:
        raise HTTPException(409, message)
    return {"ok": True, "message": message}


@router.post("/topology/devices/{device_id}/delete")
async def device_delete(device_id: int, request: Request, user: User = Depends(require("topology.edit")),
                        db: Session = Depends(get_db)):
    device = _device(db, device_id)
    if device.server:
        # Устройство сервера — удаление сервера везде: нужно и право на серверы
        if not user.can("servers.edit"):
            raise HTTPException(403, "Это устройство сервера: удалить его может только пользователь с правом на серверы")
        what = f"сервер «{device.server.name}» с карты сети (везде, в архив)"
    else:
        what = f"устройство «{device.name}» с карты сети"
    await agent_remote.before_delete(db, request, device, user)
    await agents.close_nodes(infra.delete_device(db, device, user))
    audit.log_event(db, "delete", request=request, username=user.username, detail=what)
    return RedirectResponse("/topology", 303)


@router.post("/topology/links")
async def link_create(request: Request, user: User = Depends(require("topology.edit")),
                      db: Session = Depends(get_db)):
    form = await request.form()
    a_raw, b_raw = str(form.get("a_id", "")), str(form.get("b_id", ""))
    if not (a_raw.isdigit() and b_raw.isdigit()):
        raise HTTPException(400, "Выберите два разных устройства")
    _make_link(db, int(a_raw), int(b_raw), str(form.get("a_port", "")), str(form.get("b_port", "")),
               str(form.get("kind", "wired")), str(form.get("label", "")))
    return RedirectResponse("/topology", 303)


def _make_link(db: Session, a_id: int, b_id: int, a_port: str, b_port: str, kind: str, label: str) -> Link:
    if a_id == b_id:
        raise HTTPException(400, "Выберите два разных устройства")
    if kind not in LINK_KINDS:
        raise HTTPException(400, "Неизвестный тип связи")
    a, b = _device(db, a_id), _device(db, b_id)
    a_port, b_port = a_port.strip()[:32], b_port.strip()[:32]
    if kind == "wired":
        # Занятость проверяем только для портов, заданных у устройства; свободный текст (старые связи) не трогаем
        links = db.scalars(select(Link)).all()
        for dev, port in ((a, a_port), (b, b_port)):
            if port and port in ports_svc.parse_ports(dev.ports) and port in ports_svc.used_ports(links, dev.id):
                raise HTTPException(409, f"Порт {port} устройства «{dev.name}» уже занят")
    link = Link(a_id=a.id, b_id=b.id, a_port=a_port, b_port=b_port, kind=kind, label=label.strip()[:128])
    db.add(link)
    db.commit()
    return link


class LinkIn(BaseModel):
    a_id: int
    b_id: int
    a_port: str = Field("", max_length=64)
    b_port: str = Field("", max_length=64)
    kind: str = "wired"
    label: str = Field("", max_length=256)


@router.post("/api/topology/links")
def api_link_create(body: LinkIn, user: User = Depends(require("topology.edit")), db: Session = Depends(get_db)):
    """Связь, протянутая на карте: JSON вместо формы, в ответ — готовое ребро и новая занятость портов."""
    lk = _make_link(db, body.a_id, body.b_id, body.a_port, body.b_port, body.kind, body.label)
    return {"id": lk.id, "source": lk.a_id, "target": lk.b_id, "a_port": lk.a_port, "b_port": lk.b_port,
            "label": lk.label, "kind": lk.kind}


def _delete_link(db: Session, link_id: int) -> None:
    link = db.get(Link, link_id)
    if not link:
        raise HTTPException(404, "Связь не найдена")
    db.delete(link)
    db.commit()


@router.post("/topology/links/{link_id}/delete")
def link_delete(link_id: int, user: User = Depends(require("topology.edit")), db: Session = Depends(get_db)):
    _delete_link(db, link_id)
    return RedirectResponse("/topology", 303)


@router.post("/api/topology/links/{link_id}/delete")
def api_link_delete(link_id: int, user: User = Depends(require("topology.edit")), db: Session = Depends(get_db)):
    _delete_link(db, link_id)
    return {"ok": True}


# --- Сегменты ---

class SegmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    color: str = Field("#3b82f6", pattern=r"^#[0-9a-fA-F]{6}$")
    subnet: str = Field("", max_length=64)
    description: str = Field("", max_length=500)


class DeviceSegmentIn(BaseModel):
    segment_id: int | None = None


@router.post("/api/topology/segments")
def segment_create(body: SegmentIn, user: User = Depends(require("topology.edit")), db: Session = Depends(get_db)):
    s = Segment(**body.model_dump())
    s.name = s.name.strip()
    db.add(s)
    db.commit()
    return _segment_json(s)


@router.post("/api/topology/segments/{segment_id}")
def segment_update(segment_id: int, body: SegmentIn, user: User = Depends(require("topology.edit")),
                   db: Session = Depends(get_db)):
    s = _segment(db, segment_id)
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    db.commit()
    return _segment_json(s)


@router.post("/api/topology/segments/{segment_id}/delete")
def segment_delete(segment_id: int, user: User = Depends(require("topology.edit")), db: Session = Depends(get_db)):
    """Удаляет только сегмент: устройства остаются без сегмента (SET NULL вручную — в БД нет FK)."""
    s = _segment(db, segment_id)
    for d in db.scalars(select(Device).where(Device.segment_id == s.id)):
        d.segment_id = None
    db.delete(s)
    db.commit()
    return {"ok": True}


@router.post("/api/topology/devices/{device_id}/segment")
def device_set_segment(device_id: int, body: DeviceSegmentIn, user: User = Depends(require("topology.edit")),
                       db: Session = Depends(get_db)):
    """Перенос устройства в сегмент (или из него, segment_id = null) — перетаскиванием на карте."""
    device = _device(db, device_id)
    device.segment_id = _segment(db, body.segment_id).id if body.segment_id is not None else None
    db.commit()
    return {"id": device.id, "segment_id": device.segment_id}
