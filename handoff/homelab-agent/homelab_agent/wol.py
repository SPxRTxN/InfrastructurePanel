"""Wake-on-LAN: панель просит разбудить устройство в сети узла, агент шлёт magic packet по UDP.

Работает только при `[wol] enabled = true` в конфиге узла. От панели принимается лишь MAC и широковещательный
адрес IPv4; произвольные адреса не принимаются, чтобы агент нельзя было использовать как источник UDP в чужую
сеть: адрес должен быть широковещательным (255.255.255.255 или оканчиваться на .255).
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass

log = logging.getLogger(__name__)

WOL_KEYS = {"enabled", "port"}
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


@dataclass
class WolConfig:
    enabled: bool = False
    port: int = 9


def parse_wol(raw: dict | None) -> WolConfig:
    raw = dict(raw or {})
    unknown = set(raw) - WOL_KEYS
    if unknown:
        raise ValueError(f"[wol]: неизвестные ключи: {', '.join(sorted(unknown))}")
    cfg = WolConfig(**raw)
    if not isinstance(cfg.enabled, bool):
        raise ValueError("[wol]: enabled — true/false")
    if not isinstance(cfg.port, int) or isinstance(cfg.port, bool) or cfg.port not in (7, 9):
        raise ValueError("[wol]: port — 7 или 9")
    return cfg


def magic_packet(mac: str) -> bytes:
    """6 байт 0xFF и 16 повторов MAC."""
    mac = mac.strip().lower() if isinstance(mac, str) else ""
    if not MAC_RE.match(mac):
        raise ValueError("MAC должен быть вида aa:bb:cc:dd:ee:ff")
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def check_broadcast(addr) -> str:
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        raise ValueError("broadcast — адрес IPv4") from None
    if not (str(ip) == "255.255.255.255" or str(ip).endswith(".255")):
        raise ValueError("broadcast должен быть широковещательным адресом (…255)")
    return str(ip)


class WolSender:
    def __init__(self, cfg: WolConfig):
        self.cfg = cfg

    def send(self, mac: str, broadcast: str) -> None:
        packet = magic_packet(mac)
        target = check_broadcast(broadcast)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, (target, self.cfg.port))
        finally:
            sock.close()

    async def handle(self, msg: dict) -> dict:
        """Сообщение панели `wol` → ответ `wol_result`."""
        wid = msg.get("id") if isinstance(msg.get("id"), str) and len(msg["id"]) <= 64 else ""
        try:
            self.send(msg.get("mac"), msg.get("broadcast") or "255.255.255.255")
        except ValueError as exc:
            return {"type": "wol_result", "id": wid, "ok": False, "detail": str(exc)}
        except OSError as exc:
            log.warning("wol: %s", exc)
            return {"type": "wol_result", "id": wid, "ok": False, "detail": f"сеть: {exc.strerror or 'ошибка'}"}
        log.info("wol: отправлен пакет для %s", msg.get("mac"))
        return {"type": "wol_result", "id": wid, "ok": True, "detail": f"magic packet отправлен на {msg.get('mac')}"}
