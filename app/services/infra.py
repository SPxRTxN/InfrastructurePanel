"""Удаление «в одном месте — везде» и архив удалённого.

Сервер, его узлы на карте сети и узел с агентом — один объект в трёх видах. Удаление из любого раздела
(«Серверы», «Узлы», «Карта сети») убирает всё связанное, а перед этим кладёт снимок с историей в `archives`:
его можно скачать в «Инфраструктура → Архив» в течение `history.retention_days` дней (и он попадает в бэкапы БД).
"""
import gzip
import json
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models import (AlertLog, Archive, Device, Link, Node, NodeAction, NodeEvent, NodeSample, NodeTombstone, Server, ServerMetric,
                        User)
from app.services.settings_registry import get_setting
from app.utils import utcnow


def retention(db: Session) -> timedelta:
    return timedelta(days=get_setting(db, "history.retention_days"))


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


# ---------- Снимки (без секретов: ни пути к ключу, ни хеша токена) ----------

def server_history(db: Session, server: Server, since: datetime) -> list[dict]:
    rows = db.scalars(select(ServerMetric).where(ServerMetric.server_id == server.id, ServerMetric.ts >= since)
                      .order_by(ServerMetric.ts)).all()
    return [{"ts": _ts(r.ts), "online": r.online, "ssh_ok": r.ssh_ok, "ram_total": r.ram_total,
             "ram_used": r.ram_used, "ram_free": r.ram_free, "disks": json.loads(r.disks)} for r in rows]


def node_history(db: Session, node: Node, since: datetime) -> list[dict]:
    rows = db.scalars(select(NodeSample).where(NodeSample.node_id == node.id, NodeSample.ts >= since)
                      .order_by(NodeSample.ts)).all()
    return [{"ts": _ts(r.ts), "values": json.loads(r.values_json)} for r in rows]


def server_snapshot(db: Session, server: Server, since: datetime) -> dict:
    return {
        "id": server.id, "name": server.name, "host": server.host, "ssh_port": server.ssh_port,
        "collector": server.collector, "ssh_user": server.ssh_user, "enabled": server.enabled,
        "services": [{"name": s.name, "check_type": s.check_type, "target": s.target, "ok": s.ok}
                     for s in server.services],
        "history": server_history(db, server, since),
    }


def node_snapshot(db: Session, node: Node, since: datetime) -> dict:
    actions = db.scalars(select(NodeAction).where(NodeAction.node_id == node.id).order_by(NodeAction.id)).all()
    events = db.scalars(select(NodeEvent).where(NodeEvent.node_id == node.id).order_by(NodeEvent.ts)).all()
    return {
        "id": node.id, "name": node.name, "version": node.version, "info": node.info, "ip": node.ips,
        "sensors": node.sensors, "actions_declared": node.actions, "last_seen": _ts(node.last_seen),
        "history": node_history(db, node, since),
        "actions": [{"name": a.name, "requested_at": _ts(a.requested_at), "status": a.status, "code": a.code,
                     "output": a.output, "error": a.error} for a in actions],
        "events": [{"ts": _ts(e.ts), "level": e.level, "text": e.text} for e in events],
    }


def device_snapshot(db: Session, device: Device) -> dict:
    links = db.scalars(select(Link).where(or_(Link.a_id == device.id, Link.b_id == device.id))).all()
    return {"name": device.name, "type": device.type, "ip": device.ip, "description": device.description,
            "x": device.x, "y": device.y,
            "links": [{"a": lk.a.name, "a_port": lk.a_port, "b": lk.b.name, "b_port": lk.b_port, "label": lk.label}
                      for lk in links]}


def _store(db: Session, kind: str, name: str, summary: str, user: User | None, payload: dict) -> Archive:
    raw = json.dumps(payload, ensure_ascii=False).encode()
    archive = Archive(kind=kind, name=name[:128], summary=summary[:500], deleted_by=user.username if user else "",
                      size=len(raw), data=gzip.compress(raw, compresslevel=6))
    db.add(archive)
    return archive


def unpack(archive: Archive) -> bytes:
    return gzip.decompress(archive.data)


# ---------- Удаление ----------

def delete_server(db: Session, server: Server, user: User | None) -> list[int]:
    """Сервер + сервисы + история + узлы карты сети (со связями) + узлы с агентом. → id удалённых узлов-агентов
    (их соединения закрывает вызывающий)."""
    since = utcnow() - retention(db)
    nodes = db.scalars(select(Node).where(Node.server_id == server.id)).all()
    devices = db.scalars(select(Device).where(Device.server_id == server.id)).all()
    payload = {"kind": "server", "deleted_at": _ts(utcnow()), "server": server_snapshot(db, server, since),
               "nodes": [node_snapshot(db, n, since) for n in nodes],
               "devices": [device_snapshot(db, d) for d in devices]}
    parts = [f"сервисов {len(server.services)}", f"точек истории {len(payload['server']['history'])}"]
    if nodes:
        parts.append("узел с агентом: " + ", ".join(n.name for n in nodes))
    if devices:
        parts.append("на карте: " + ", ".join(d.name for d in devices))
    _store(db, "server", server.name, "; ".join(parts), user, payload)
    node_ids = [n.id for n in nodes]
    for obj in (*nodes, *devices, server):
        db.delete(obj)
    db.commit()
    return node_ids


def delete_node(db: Session, node: Node, user: User | None) -> list[int]:
    """Узел, привязанный к серверу, удаляется вместе с сервером («везде»); отдельный — сам, с архивом."""
    if node.server:
        return delete_server(db, node.server, user)
    since = utcnow() - retention(db)
    snap = node_snapshot(db, node, since)
    _store(db, "node", node.name, f"точек истории {len(snap['history'])}", user,
           {"kind": "node", "deleted_at": _ts(utcnow()), "nodes": [snap]})
    node_id = node.id
    db.delete(node)
    db.commit()
    return [node_id]


def delete_device(db: Session, device: Device, user: User | None) -> list[int]:
    """Устройство сервера — удаление сервера везде; просто устройство — только оно со связями."""
    if device.server:
        return delete_server(db, device.server, user)
    db.delete(device)
    db.commit()
    return []


def what_goes(db: Session, server: Server) -> str:
    """Текст подтверждения: что ещё исчезнет вместе с сервером."""
    extra = [f"узел с агентом «{n.name}»" for n in db.scalars(select(Node).where(Node.server_id == server.id))]
    extra += [f"«{d.name}» на карте сети" for d in db.scalars(select(Device).where(Device.server_id == server.id))]
    if server.services:
        extra.append(f"сервисы ({len(server.services)})")
    tail = (" Вместе с ним: " + ", ".join(extra) + ".") if extra else ""
    return f"Удалить сервер «{server.name}» везде?{tail} История попадёт в архив."


def cleanup(db: Session) -> None:
    """История серверов и узлов и архив удалённого — не старше `history.retention_days`."""
    border = utcnow() - retention(db)
    db.execute(delete(ServerMetric).where(ServerMetric.ts < border))
    db.execute(delete(NodeSample).where(NodeSample.ts < border))
    db.execute(delete(Archive).where(Archive.deleted_at < border))
    db.execute(delete(AlertLog).where(AlertLog.ts < border))
    db.execute(delete(NodeTombstone).where(NodeTombstone.expires_at < utcnow()))
    db.commit()
