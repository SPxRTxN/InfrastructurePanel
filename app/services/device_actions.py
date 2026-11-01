"""Действия над устройствами карты сети по их типу.

Реестр ACTIONS: новое действие = одна запись `Action` (+ обработчик, если это не ссылка). Действие доступно, если
тип устройства подходит, выполнено условие (`needs`: ip / mac / server) и у пользователя есть право `perm`.

Wake-on-LAN идёт через агента в той же сети: панель шлёт `wol` только онлайн-узлу, который сам объявил
`features: ["wol"]` (включил `[wol]` в конфиге), и ждёт `wol_result`.
"""
import asyncio
import itertools
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Device, Node, User
from app.services import agents

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
WOL_TIMEOUT = 8.0  # секунд ждать wol_result от агента


class ActionError(Exception):
    """Понятная пользователю причина отказа (HTTP 409/400)."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message, self.status = message, status


def normalize_mac(raw: str) -> str:
    """'AA:BB:CC:DD:EE:FF' (или через '-') → 'aa:bb:cc:dd:ee:ff'; пусто → пусто; иное — ValueError."""
    text = (raw or "").strip().lower().replace("-", ":")
    if text and not MAC_RE.match(text):
        raise ValueError("MAC-адрес должен быть вида aa:bb:cc:dd:ee:ff")
    return text


@dataclass
class Action:
    id: str
    label: str
    types: set[str]
    needs: str = ""          # "", "ip", "mac", "server"
    perm: str = "topology.view"
    href: Callable[[Device], str] | None = None   # ссылка — открывается в браузере, POST не нужен
    handler: Callable[..., Awaitable[str]] | None = None  # async (db, device, user, form) -> сообщение


def _wol_agents(db: Session) -> list[Node]:
    """Онлайн-узлы с соединением и возможностью wol."""
    return [n for n in db.scalars(select(Node).order_by(Node.name))
            if "wol" in n.info.get("features", []) and agents.is_online(db, n) and n.id in agents.hub.sockets]


_ids = itertools.count(1)
_waiters: dict[str, tuple[int, asyncio.Future]] = {}


def on_wol_result(node: Node, data: dict) -> None:
    """Вызывается из WebSocket агента: ответ принимается только от узла, которому мы слали запрос."""
    entry = _waiters.get(str(data.get("id", "")))
    if entry and entry[0] == node.id and not entry[1].done():
        entry[1].set_result((data.get("ok") is True, str(data.get("detail", ""))[:300]))


async def _wol(db: Session, device: Device, user: User, form) -> str:
    candidates = _wol_agents(db)
    if not candidates:
        raise ActionError("Нет агента с включённым Wake-on-LAN в сети: включите [wol] enabled = true "
                          "в конфиге агента на узле в той же сети, что и устройство")
    node_raw = str(form.get("node_id", ""))
    if node_raw:
        node = next((n for n in candidates if str(n.id) == node_raw), None)
        if not node:
            raise ActionError("Выбранный агент не на связи или не умеет Wake-on-LAN")
    else:
        node = candidates[0]
    wid = f"w-{next(_ids)}"
    fut = asyncio.get_running_loop().create_future()
    _waiters[wid] = (node.id, fut)
    try:
        if not await agents.hub.send(node.id, {"type": "wol", "id": wid, "mac": device.mac,
                                               "broadcast": "255.255.255.255"}):
            raise ActionError("Не удалось отправить команду агенту")
        try:
            ok, detail = await asyncio.wait_for(fut, WOL_TIMEOUT)
        except asyncio.TimeoutError:
            raise ActionError(f"Агент «{node.name}» не ответил") from None
    finally:
        _waiters.pop(wid, None)
    if not ok:
        raise ActionError(f"Агент «{node.name}» отказал: {detail or 'ошибка'}")
    return f"Пакет пробуждения отправлен через агента «{node.name}»" + (f": {detail}" if detail else "")


ACTIONS: dict[str, Action] = {a.id: a for a in [
    Action("web", "Открыть веб-интерфейс", {"router", "ap", "switch", "iot", "server", "pc"}, needs="ip",
           href=lambda d: f"http://{d.ip}"),
    Action("wol", "Разбудить (Wake-on-LAN)", {"pc", "server"}, needs="mac", perm="topology.edit",
           handler=_wol),
    Action("server_page", "Страница сервера", {"router", "ap", "switch", "iot", "server", "pc", "other"},
           needs="server", perm="servers.view", href=lambda d: f"/servers/{d.server_id}"),
    Action("node_page", "Страница узла", {"server", "pc", "other"}, needs="node", perm="servers.view",
           href=lambda d: ""),
]}


def _met(db: Session, device: Device, need: str) -> str | None:
    """Для need возвращает значение (ссылку-основу) или None, если условие не выполнено."""
    if need == "":
        return "1"
    if need == "ip":
        return device.ip if re.fullmatch(r"[0-9A-Za-z.:\-]{1,64}", device.ip or "") else None
    if need == "mac":
        return device.mac or None
    if need == "server":
        return str(device.server_id) if device.server_id else None
    if need == "node":
        node = db.scalar(select(Node).where(Node.server_id == device.server_id)) if device.server_id else None
        return str(node.id) if node else None
    return None


def available(db: Session, device: Device, user: User) -> list[dict]:
    """Действия, доступные пользователю для устройства: для рендера кнопок."""
    out = []
    for a in ACTIONS.values():
        if device.type not in a.types or not user.can(a.perm) or _met(db, device, a.needs) is None:
            continue
        item = {"id": a.id, "label": a.label, "kind": "link" if a.href else "post"}
        if a.id == "node_page":
            item["href"] = f"/nodes/{_met(db, device, 'node')}"
        elif a.href:
            item["href"] = a.href(device)
            item["external"] = a.id == "web"
        if a.id == "wol":
            item["agents"] = [{"id": n.id, "name": n.name} for n in _wol_agents(db)]
        out.append(item)
    return out


async def run(db: Session, device: Device, action_id: str, user: User, form) -> str:
    a = ACTIONS.get(action_id)
    if not a or not a.handler or device.type not in a.types:
        raise ActionError("Такого действия для этого устройства нет", 404)
    if not user.can(a.perm):
        raise ActionError("Недостаточно прав", 403)
    if _met(db, device, a.needs) is None:
        raise ActionError("Для действия не хватает данных устройства (например, MAC-адреса)", 400)
    return await a.handler(db, device, user, form)
