"""Управление агентом из панели: настройки (config_get/config_set) и удаление (uninstall).

Протокол — handoff/homelab-agent/docs/Протокол.md (раздел «Настройки и удаление из панели»). Ожидание ответа устроено как у wol_result
(device_actions): запрос с id, будущий результат, ответ принимается только от узла, которому слали запрос.
Окончательная проверка настроек — на агенте; панель проверяет только размер и структуру.
"""
import asyncio
import itertools
import json
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Node, NodeTombstone, Server, User
from app.services import agents, audit
from app.services.security import hash_token
from app.utils import utcnow

CONFIG_TIMEOUT = 8.0
UNINSTALL_TIMEOUT = 10.0
TOMBSTONE_DAYS = 30
MAX_SECTION = 20_000
# Разделы, которые панель вообще вправе слать (агент сам сообщает свой список managed и проверяет его)
SECTIONS = ("interval", "collectors", "display", "wol", "update", "terminal")

_ids = itertools.count(1)
_waiters: dict[str, tuple[int, asyncio.Future]] = {}


class RemoteError(Exception):
    """Понятная причина отказа для пользователя."""


def supports(db: Session, node: Node, feature: str) -> str:
    """'' — можно; иначе причина, почему нельзя."""
    if feature not in node.info.get("features", []):
        return "Агент не сообщил, что умеет это (обновите агента или включите возможность в его конфиге)"
    if not agents.is_online(db, node) or node.id not in agents.hub.sockets:
        return "Узел не на связи"
    return ""


def on_message(node: Node, data: dict) -> None:
    """Из WebSocket агента: config / config_result / uninstall_result."""
    entry = _waiters.get(str(data.get("id", "")))
    if entry and entry[0] == node.id and not entry[1].done():
        entry[1].set_result(data)


async def ask(node: Node, prefix: str, message: dict, timeout: float) -> dict:
    rid = f"{prefix}-{next(_ids)}"
    fut = asyncio.get_running_loop().create_future()
    _waiters[rid] = (node.id, fut)
    try:
        if not await agents.hub.send(node.id, {**message, "id": rid}):
            raise RemoteError("Не удалось отправить команду агенту")
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RemoteError(f"Агент «{node.name}» не ответил") from None
    finally:
        _waiters.pop(rid, None)


# ---------- Настройки ----------

async def get_config(db: Session, node: Node) -> dict:
    why = supports(db, node, "config")
    if why:
        raise RemoteError(why)
    data = await ask(node, "c", {"type": "config_get"}, CONFIG_TIMEOUT)
    if data.get("type") != "config" or not isinstance(data.get("sections"), dict):
        raise RemoteError("Агент прислал неожиданный ответ")
    managed = [m for m in data.get("managed", []) if isinstance(m, str)]
    sections = data["sections"]
    return {"managed": managed,
            "sections": {m: json.dumps(sections.get(m, {}), ensure_ascii=False, indent=2) for m in managed},
            "raw": {m: sections.get(m) for m in managed},  # для формы с полями (agent_form.view)
            "local_only": [x for x in data.get("local_only", []) if isinstance(x, str)]}


def parse_sections(form, managed: list[str] | None = None) -> dict:
    """Поля формы `sec_<раздел>` (JSON-объект) → словарь разделов. Только известные разделы, размер ограничен."""
    allowed = set(managed) if managed else set(SECTIONS)
    out = {}
    for key, raw in form.multi_items():
        if not key.startswith("sec_"):
            continue
        name = key[4:]
        if name not in allowed:
            raise RemoteError(f"Раздел «{name}» нельзя менять из панели")
        text = str(raw)
        if len(text) > MAX_SECTION:
            raise RemoteError(f"Раздел «{name}» слишком большой")
        try:
            value = json.loads(text or "{}")
        except ValueError:
            raise RemoteError(f"Раздел «{name}»: это не JSON") from None
        if not isinstance(value, dict):
            raise RemoteError(f"Раздел «{name}»: нужен JSON-объект {{…}}")
        if name == "collectors" and "command" in value:
            raise RemoteError("collectors.command меняется только на узле")
        out[name] = value
    if not out:
        raise RemoteError("Нечего сохранять")
    return out


async def set_config(db: Session, node: Node, sections: dict) -> tuple[bool, str]:
    why = supports(db, node, "config")
    if why:
        raise RemoteError(why)
    data = await ask(node, "c", {"type": "config_set", "sections": sections}, CONFIG_TIMEOUT)
    return data.get("ok") is True, str(data.get("detail", ""))[:300]


# ---------- Удаление агента вместе с узлом ----------

def affected_nodes(db: Session, obj) -> list[Node]:
    """Узлы, которые исчезнут при удалении узла / сервера / устройства «везде»."""
    if isinstance(obj, Node):
        return list(db.scalars(select(Node).where(Node.server_id == obj.server_id))) if obj.server_id else [obj]
    server_id = obj.id if isinstance(obj, Server) else getattr(obj, "server_id", None)
    return list(db.scalars(select(Node).where(Node.server_id == server_id))) if server_id else []


def wanted(form) -> bool:
    """Галочка «удалить агента с узла»: по умолчанию включена, снята — '0'."""
    return str(form.get("remove_agent", "1")) != "0"


async def remove_agents(db: Session, nodes: list[Node], user: User) -> list[str]:
    """Перед удалением узлов: онлайн и умеет uninstall — команда и ожидание ответа; иначе (или нет ответа) —
    надгробие. → короткие итоги для журнала."""
    notes = []
    for node in nodes:
        name, token_hash = node.name, node.token_hash
        result = ""
        if not supports(db, node, "uninstall"):
            try:
                data = await ask(node, "x", {"type": "uninstall"}, UNINSTALL_TIMEOUT)
                if data.get("ok") is True:
                    notes.append(f"агент «{name}» удаляется с узла")
                    continue
                result = f"агент отказал: {str(data.get('detail', ''))[:100] or 'ошибка'}"
                notes.append(f"«{name}»: {result}")
                continue  # агент ответил отказом (uninstall выключен в его конфиге) — надгробие бессмысленно
            except RemoteError as e:
                result = str(e)
        else:
            result = "не на связи или не умеет uninstall"
        db.add(NodeTombstone(name=name[:64], token_hash=token_hash, expires_at=utcnow() + timedelta(days=TOMBSTONE_DAYS)))
        db.commit()
        notes.append(f"«{name}»: {result}; агент будет удалён при возвращении на связь (до {TOMBSTONE_DAYS} дн.)")
    return notes


async def before_delete(db: Session, request, obj, user: User) -> str:
    """Для обработчиков удаления узла/сервера/устройства: если галочка включена — убрать агентов; итог в журнал."""
    if not wanted(await request.form()):
        return ""
    notes = await remove_agents(db, affected_nodes(db, obj), user)
    text = "; ".join(notes)
    if text:
        log(db, user, request, text)
    return text


def find_tombstone(db: Session, token: str) -> NodeTombstone | None:
    return db.scalar(select(NodeTombstone).where(NodeTombstone.token_hash == hash_token(token),
                                                 NodeTombstone.expires_at >= utcnow()))


def log(db: Session, user: User | None, request, detail: str, kind: str = "agent_uninstall", ok: bool = True) -> None:
    audit.log_event(db, kind, request=request, username=user.username if user else "", ok=ok, detail=detail)
