"""Приём агентов homelab-agent (протокол v1 — handoff/homelab-agent/docs/Протокол.md).

Агент сам подключается по WebSocket и описывает себя в `hello`; панель хранит последние значения,
прореженную историю, запросы действий и события. Панель **не присылает команд**: только имя действия
из списка, который агент объявил в `hello` (allowlist хранится на узле).
"""
import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import WebSocket
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import Node, NodeAction, NodeEvent, NodeSample, User
from app.services.security import hash_token, new_token, tokens_equal
from app.services.settings_registry import get_setting
from app.utils import utcnow

PROTOCOL_VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
MAX_MESSAGE = 256 * 1024
MAX_SENSORS = 1000
MAX_ACTIONS = 50
HISTORY = timedelta(hours=24)
ACTION_TIMEOUT = timedelta(minutes=5)
# События «агент переподключился» (читает оповещение agent_restarted): обрыв от 2 мин или смена версии
RESTART_MARK = "Агент подключился снова: "
RESTART_MARK_MINUTES = 2


def interval(db: Session) -> int:
    return get_setting(db, "agents.interval")


def is_online(db: Session, node: Node, now: datetime | None = None) -> bool:
    """Онлайн — соединение открыто и данные приходили не дольше трёх интервалов назад."""
    now = now or utcnow()
    return bool(node.connected and node.last_seen and now - node.last_seen <= timedelta(seconds=3 * interval(db)))


# ---------- Токены ----------

def issue_token(node: Node) -> str:
    """Новый токен узла: в БД — только хеш, сам токен показывается один раз."""
    token = new_token()
    node.token_hash = hash_token(token)
    return token


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


# ---------- Разбор сообщений ----------

def _str(v, limit: int) -> str:
    return v[:limit] if isinstance(v, str) else ""


def parse_hello(data) -> dict | str:
    if not isinstance(data, dict) or data.get("type") != "hello":
        return "expected hello"
    node, token = data.get("node"), data.get("token")
    if not isinstance(node, str) or not valid_name(node):
        return "bad node"
    if not isinstance(token, str) or not token or len(token) > 200:
        return "bad token"
    sensors = data.get("sensors", [])
    if not isinstance(sensors, list) or len(sensors) > MAX_SENSORS:
        return "bad sensors"
    parsed = []
    for s in sensors:
        if not (isinstance(s, dict) and isinstance(s.get("id"), str) and 0 < len(s["id"]) <= 200
                and isinstance(s.get("kind"), str) and KIND_RE.match(s["kind"])):
            return "bad sensor"
        parsed.append({"id": s["id"], "kind": s["kind"], "label": _str(s.get("label"), 300) or s["id"],
                       "unit": _str(s.get("unit"), 16)})
    actions = []
    for a in (data.get("actions") or [])[:MAX_ACTIONS]:
        if isinstance(a, dict) and isinstance(a.get("name"), str) and NAME_RE.match(a["name"]):
            actions.append({"name": a["name"], "label": _str(a.get("label"), 120) or a["name"]})
    info_raw = data.get("info") if isinstance(data.get("info"), dict) else {}
    info = {k: _str(info_raw.get(k), 200) for k in ("os", "kernel", "hostname", "cpu") if isinstance(info_raw.get(k), str)}
    for k in ("threads", "memory_kb", "boot_ts"):
        if isinstance(info_raw.get(k), (int, float)) and math.isfinite(info_raw[k]):
            info[k] = info_raw[k]
    # Возможности, которые владелец включил в конфиге агента (например, "vpn") — панель без них не шлёт команд
    features = data.get("features") if isinstance(data.get("features"), list) else []
    info["features"] = sorted({f for f in features if isinstance(f, str) and NAME_RE.match(f)})[:20]
    ips = [ip[:64] for ip in (data.get("ip") or [])[:8] if isinstance(ip, str)]
    return {"node": node, "token": token, "version": _str(data.get("version"), 32), "info": info,
            "sensors": parsed, "actions": actions, "ip": ips}


def parse_values(data) -> dict[str, float]:
    raw = data.get("values") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {k[:200]: float(v) for k, v in list(raw.items())[:MAX_SENSORS * 2]
            if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}


def sample_time(data) -> datetime:
    """Время выборки агента (unix ms), но не из будущего и не старше суток — иначе время приёма."""
    now = utcnow()
    ts = data.get("ts") if isinstance(data, dict) else None
    if isinstance(ts, (int, float)) and math.isfinite(ts):
        t = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).replace(tzinfo=None)
        if now - HISTORY < t <= now + timedelta(seconds=60):
            return min(t, now)
    return now


# ---------- Обработка ----------

def handle_hello(db: Session, hello: dict, remote_ip: str) -> Node | str:
    node = db.scalar(select(Node).where(Node.name == hello["node"]))
    # Узел заводится в панели заранее; неизвестное имя и неверный токен — одинаковый ответ
    if not node or not tokens_equal(node.token_hash, hash_token(hello["token"])):
        return "bad token"
    known = {s["id"] for s in hello["sensors"]}
    now = utcnow()
    # Хук для оповещения agent_restarted: обрыв или смена версии — запись в события узла (протокол не меняется)
    if node.last_seen:
        gap = int((now - node.last_seen).total_seconds() // 60)
        parts = ([f"обрыв {gap} мин"] if gap >= RESTART_MARK_MINUTES else []) + (
            [f"версия {node.version} → {hello['version']}"] if node.version and node.version != hello["version"] else [])
        if parts:
            db.add(NodeEvent(node_id=node.id, ts=now, level="info", text=RESTART_MARK + ", ".join(parts)))
    node.connected, node.last_seen, node.remote_ip = True, now, remote_ip[:64]
    node.version = hello["version"]
    node.info_json = json.dumps(hello["info"], ensure_ascii=False)
    node.sensors_json = json.dumps(hello["sensors"], ensure_ascii=False)
    node.actions_json = json.dumps(hello["actions"], ensure_ascii=False)
    node.ips_json = json.dumps(hello["ip"])
    # Датчики, которых больше нет, не висят в последних значениях
    node.values_json = json.dumps({k: v for k, v in node.values.items() if k in known})
    db.commit()
    return node


def handle_sample(db: Session, node: Node, data) -> None:
    values = parse_values(data)
    ts = sample_time(data)
    now = utcnow()
    node.last_seen = now
    # Выборка из буфера после обрыва старее последней — только в историю
    if not node.values_ts or ts >= node.values_ts:
        node.values_json = json.dumps({**node.values, **values})
        node.values_ts = ts
    last = db.scalar(select(NodeSample.ts).where(NodeSample.node_id == node.id).order_by(NodeSample.ts.desc()).limit(1))
    every = timedelta(seconds=get_setting(db, "agents.history_every"))
    if values and (last is None or abs(ts - last) >= every):
        db.add(NodeSample(node_id=node.id, ts=ts, values_json=json.dumps(values)))
    db.commit()


def handle_action_result(db: Session, node: Node, data) -> None:
    action_id = str(data.get("id", "")).removeprefix("a-")
    if not action_id.isdigit():
        return
    action = db.get(NodeAction, int(action_id))
    if not action or action.node_id != node.id or action.status != "sent":
        return
    action.status = "ok" if data.get("ok") is True else "failed"
    action.code = data["code"] if isinstance(data.get("code"), int) else None
    action.output = _str(data.get("output"), 4096)
    action.error = _str(data.get("error"), 200)
    action.finished_at = utcnow()
    db.commit()


def handle_event(db: Session, node: Node, data) -> None:
    level = data.get("level") if data.get("level") in ("info", "warning", "error") else "info"
    text = _str(data.get("text"), 500)
    if text:
        db.add(NodeEvent(node_id=node.id, ts=sample_time(data), level=level, text=text))
        db.commit()


def disconnect(db: Session, node_id: int) -> None:
    db.execute(update(Node).where(Node.id == node_id).values(connected=False))
    db.commit()


# ---------- Соединения и действия ----------

class Hub:
    """Открытые соединения агентов (один процесс приложения — словаря достаточно)."""

    def __init__(self):
        self.sockets: dict[int, WebSocket] = {}

    def register(self, node_id: int, ws: WebSocket) -> WebSocket | None:
        old = self.sockets.get(node_id)
        self.sockets[node_id] = ws
        return old

    def unregister(self, node_id: int, ws: WebSocket) -> bool:
        """True, если сняли именно это соединение (а не уже заменённое новым)."""
        if self.sockets.get(node_id) is ws:
            del self.sockets[node_id]
            return True
        return False

    async def send(self, node_id: int, message: dict) -> bool:
        ws = self.sockets.get(node_id)
        if not ws:
            return False
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
            return True
        except Exception:  # соединение уже закрыто — узел станет офлайн при уборке
            return False


hub = Hub()


async def close_nodes(node_ids: list[int]) -> None:
    """Закрыть соединения удалённых узлов (или узлов со сменённым токеном) — агент переподключится и получит отказ."""
    for node_id in node_ids:
        ws = hub.sockets.get(node_id)
        if ws:
            try:
                await ws.close(code=1008)
            except RuntimeError:
                pass


async def request_action(db: Session, node: Node, name: str, user: User) -> NodeAction | str:
    if name not in {a["name"] for a in node.actions}:
        return "Узел не объявлял такого действия"
    if not is_online(db, node) or node.id not in hub.sockets:
        return "Узел не подключён"
    action = NodeAction(node_id=node.id, name=name, user_id=user.id)
    db.add(action)
    db.commit()
    if not await hub.send(node.id, {"type": "action", "id": f"a-{action.id}", "name": name}):
        action.status, action.error, action.finished_at = "failed", "не удалось отправить", utcnow()
        db.commit()
    return action


async def broadcast_summary(db: Session) -> None:
    """Сводка для экрана дисплея агентов: сколько узлов онлайн и последняя тревога."""
    nodes = db.scalars(select(Node)).all()
    online = sum(1 for n in nodes if is_online(db, n))
    event = db.scalar(select(NodeEvent).where(NodeEvent.level != "info").order_by(NodeEvent.ts.desc()).limit(1))
    message = {"type": "summary", "online": online, "total": len(nodes), "alert": event.text[:60] if event else ""}
    await asyncio.gather(*(hub.send(node_id, message) for node_id in list(hub.sockets)))


def housekeeping(db: Session) -> None:
    now = utcnow()
    stale = now - timedelta(seconds=3 * interval(db))
    db.execute(update(Node).where(Node.connected.is_(True), Node.last_seen < stale).values(connected=False))
    db.execute(update(NodeAction).where(NodeAction.status == "sent", NodeAction.requested_at < now - ACTION_TIMEOUT)
               .values(status="lost", error="нет ответа агента", finished_at=now))
    db.execute(delete(NodeEvent).where(NodeEvent.ts < now - timedelta(days=30)))
    db.commit()


# ---------- Метрики для «Серверов» ----------

@dataclass
class NodeMetrics:
    ram_total: int | None = None
    ram_used: int | None = None
    ram_free: int | None = None
    disks: list[dict] = field(default_factory=list)


def server_metrics(values: dict[str, float], sensors: list[dict] | None = None) -> NodeMetrics:
    """RAM и разделы из датчиков агента: memory.*, disks.<точка монтирования>.total|used|free.
    Метка датчика диска («/dev/sda1 ext4») попадает в поле device."""
    labels = {s["id"]: s["label"] for s in sensors or []}
    m = NodeMetrics()
    if "memory.total" in values:
        m.ram_total = int(values["memory.total"])
        avail = values.get("memory.available")
        m.ram_used = int(values.get("memory.used", m.ram_total - avail if avail is not None else 0))
        m.ram_free = int(avail) if avail is not None else m.ram_total - m.ram_used
    mounts: dict[str, dict] = {}
    for key, v in values.items():
        if key.startswith("disks.") and key.rsplit(".", 1)[-1] in ("total", "used", "free"):
            mount, metric = key[len("disks."):].rsplit(".", 1)
            mounts.setdefault(mount, {})[metric] = int(v)
    for mount in sorted(mounts):
        d = mounts[mount]
        if "total" in d:
            disk = {"mount": mount, "size": d["total"], "used": d.get("used", 0),
                    "free": d.get("free", d["total"] - d.get("used", 0))}
            device = labels.get(f"disks.{mount}.total", "")
            if device and device != f"disks.{mount}.total":
                disk["device"] = device[:120]
            m.disks.append(disk)
    return m


def history(db: Session, node: Node, kinds: set[str]) -> dict:
    """История датчиков выбранных видов: {sensors: [...], points: [{ts, values}]}."""
    sensors = [s for s in node.sensors if s["kind"] in kinds]
    ids = {s["id"] for s in sensors}
    rows = db.scalars(select(NodeSample).where(NodeSample.node_id == node.id, NodeSample.ts >= utcnow() - HISTORY)
                      .order_by(NodeSample.ts)).all()
    points = []
    for r in rows:
        values = {k: v for k, v in json.loads(r.values_json).items() if k in ids}
        if values:
            points.append({"ts": r.ts.isoformat() + "Z", "values": values})
    return {"sensors": sensors, "points": points}
