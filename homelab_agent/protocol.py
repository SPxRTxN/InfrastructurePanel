"""Сообщения протокола агент ↔ панель (v1), см. docs/Протокол.md.

Неизвестные типы и поля получатель игнорирует, поэтому здесь только
сборка исходящих сообщений и мягкий разбор входящих.
"""
from __future__ import annotations

import json

from . import __version__

VERSION = 1
MAX_OUTPUT = 4096  # байт вывода действия в action_result


def encode(msg: dict) -> str:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))


def decode(raw) -> dict | None:
    """JSON-объект с полем type (строка) или None — всё прочее игнорируется."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        return None
    return msg


def hello(node: str, token: str, sensors: list[dict], actions: list[dict], sysinfo: dict) -> dict:
    return {
        "type": "hello",
        "v": VERSION,
        "node": node,
        "token": token,
        "version": __version__,
        "mac": sysinfo.get("mac", ""),
        "ip": sysinfo.get("ip", []),
        "info": sysinfo.get("info", {}),
        "sensors": sensors,
        "actions": actions,
    }


def sample(ts_ms: int, values: dict) -> dict:
    return {"type": "sample", "ts": ts_ms, "values": values}


def truncate_output(data: bytes, limit: int = MAX_OUTPUT) -> str:
    """Обрезка по байтам без порчи UTF-8 на границе."""
    return data[:limit].decode("utf-8", errors="ignore") if len(data) > limit else data.decode("utf-8", errors="replace")


def action_result(action_id: str, name: str, ok: bool, code: int | None, output: str = "",
                  duration_ms: int = 0, error: str | None = None) -> dict:
    msg = {
        "type": "action_result",
        "id": action_id,
        "name": name,
        "ok": ok,
        "code": code,
        "output": output,
        "duration_ms": duration_ms,
    }
    if error:
        msg["error"] = error
    return msg


def event(ts_ms: int, level: str, text: str) -> dict:
    return {"type": "event", "ts": ts_ms, "level": level, "text": text}
