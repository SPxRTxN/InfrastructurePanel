"""VPN узла: панель присылает конфиг клиента sing-box, агент проверяет, применяет и при неудаче откатывается.

Работает только при `[vpn] enabled = true` в конфиге узла (владелец решает сам). Безопасность:
- принимается только то, что нужно для «весь трафик через туннель»: один inbound типа tun, outbounds известных
  типов, без `experimental` (Clash API открыл бы порт управления) и без прослушивающих inbound (socks/http/mixed);
- адреса всех панелей агент сам добавляет в обход туннеля — неудачный сервер не отрежет узел от панели;
- применение — командами из конфига узла (по умолчанию `sudo -n systemctl restart homelab-vpn.service`),
  без shell; сторож за `rollback_seconds` проверяет связь с панелью и выход в интернет, иначе — прежний конфиг.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .collectors.base import SAFE_ENV

log = logging.getLogger(__name__)

TOP_KEYS = {"log", "dns", "inbounds", "outbounds", "route"}
OUTBOUND_TYPES = {"vless", "vmess", "trojan", "shadowsocks", "hysteria2", "direct", "block", "dns"}
VPN_KEYS = {"enabled", "singbox", "config_dir", "apply_argv", "stop_argv", "check_url", "rollback_seconds"}


class VpnError(ValueError):
    pass


@dataclass
class VpnConfig:
    enabled: bool = False
    singbox: str = "/usr/local/bin/sing-box"
    config_dir: str = "/opt/homelab-agent/etc/vpn"  # = guard.ETC_DIR + "/vpn"
    apply_argv: list[str] = field(default_factory=lambda: ["sudo", "-n", "systemctl", "restart", "homelab-vpn.service"])
    stop_argv: list[str] = field(default_factory=lambda: ["sudo", "-n", "systemctl", "stop", "homelab-vpn.service"])
    check_url: str = "https://www.gstatic.com/generate_204"
    rollback_seconds: int = 60


def parse_vpn(raw: dict | None) -> VpnConfig:
    raw = dict(raw or {})
    unknown = set(raw) - VPN_KEYS
    if unknown:
        raise ValueError(f"[vpn]: неизвестные ключи: {', '.join(sorted(unknown))}")
    cfg = VpnConfig()
    for key in VPN_KEYS:
        if key in raw:
            setattr(cfg, key, raw[key])
    if not isinstance(cfg.enabled, bool):
        raise ValueError("[vpn]: enabled — true/false")
    for key in ("apply_argv", "stop_argv"):
        val = getattr(cfg, key)
        if not isinstance(val, list) or not val or not all(isinstance(x, str) for x in val):
            raise ValueError(f"[vpn]: {key} — список аргументов")
    if not isinstance(cfg.rollback_seconds, int) or not 10 <= cfg.rollback_seconds <= 600:
        raise ValueError("[vpn]: rollback_seconds — 10..600")
    return cfg


# ---------- Проверка присланного конфига ----------

def _resolve(host: str) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})
    except OSError:
        return []


def prepare(config: dict, panel_urls: list[str], resolve=_resolve) -> dict:
    """Проверить структуру и добавить адреса панелей в обход туннеля. VpnError — отказ с понятной причиной."""
    if not isinstance(config, dict) or set(config) - TOP_KEYS:
        raise VpnError("в конфиге лишние разделы (разрешены: " + ", ".join(sorted(TOP_KEYS)) + ")")
    inbounds = config.get("inbounds")
    if not isinstance(inbounds, list) or len(inbounds) != 1 or not isinstance(inbounds[0], dict) \
            or inbounds[0].get("type") != "tun":
        raise VpnError("ожидается ровно один inbound типа tun (прослушивающие прокси запрещены)")
    outbounds = config.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds:
        raise VpnError("нет outbounds")
    for ob in outbounds:
        if not isinstance(ob, dict) or ob.get("type") not in OUTBOUND_TYPES:
            raise VpnError(f"тип outbound {ob.get('type') if isinstance(ob, dict) else ob!r} не разрешён")
    tun = dict(inbounds[0])
    exclude = list(tun.get("route_exclude_address", []))
    for url in panel_urls:
        host = urlsplit(url).hostname or ""
        for ip in resolve(host) if host else []:
            cidr = f"{ip}/32" if ":" not in ip else f"{ip}/128"
            if cidr not in exclude:
                exclude.append(cidr)
    tun["route_exclude_address"] = exclude
    return {**config, "inbounds": [tun]}


# ---------- Применение со сторожем ----------

async def _run(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                    env=SAFE_ENV)
    except OSError as exc:
        return 127, f"не запустить {argv[0]}: {exc.strerror}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "таймаут"
    return proc.returncode or 0, out.decode("utf-8", "replace")[-500:]


def _tcp_ok(url: str) -> bool:
    u = urlsplit(url)
    port = u.port or (443 if u.scheme in ("wss", "https") else 80)
    try:
        with socket.create_connection((u.hostname, port), timeout=5):
            return True
    except (OSError, TypeError):
        return False


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=8) as r:  # nosec B310 — адрес из конфига узла
            return r.status < 400
    except Exception:  # noqa: BLE001 — любая ошибка = «интернета через туннель нет»
        return False


class VpnManager:
    def __init__(self, cfg: VpnConfig, panel_urls: list[str], *, resolve=_resolve, panel_ok=None, internet_ok=None,
                 poll: float = 3.0):
        self.cfg = cfg
        self.panel_urls = panel_urls
        self.resolve = resolve
        self.panel_ok = panel_ok or (lambda: any(_tcp_ok(u) for u in self.panel_urls))
        self.internet_ok = internet_ok or (lambda: _http_ok(self.cfg.check_url))
        self.poll = poll
        self._lock = asyncio.Lock()

    @property
    def path(self) -> str:
        return os.path.join(self.cfg.config_dir, "config.json")

    async def handle(self, msg: dict) -> dict:
        vid = str(msg.get("id", ""))[:64]
        if self._lock.locked():
            return {"type": "vpn_result", "id": vid, "ok": False, "detail": "уже применяется другой конфиг"}
        async with self._lock:
            try:
                detail = await (self._off() if msg.get("mode") == "off" else self._apply(msg.get("config")))
                return {"type": "vpn_result", "id": vid, "ok": True, "detail": detail}
            except VpnError as exc:
                log.warning("vpn: %s", exc)
                return {"type": "vpn_result", "id": vid, "ok": False, "detail": str(exc)[:500]}

    async def _off(self) -> str:
        code, out = await _run(self.cfg.stop_argv)
        if code:
            raise VpnError(f"не удалось остановить: {out.strip()[:200]}")
        for p in (self.path, self.path + ".prev"):
            if os.path.exists(p):
                os.remove(p)
        return "VPN выключен, трафик напрямую"

    async def _apply(self, config) -> str:
        final = prepare(config, self.panel_urls, self.resolve)
        os.makedirs(self.cfg.config_dir, mode=0o700, exist_ok=True)
        new = self.path + ".new"
        fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # в конфиге ключи сервера
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(final, f, ensure_ascii=False, indent=1)
        code, out = await _run([self.cfg.singbox, "check", "-c", new])
        if code:
            os.remove(new)
            raise VpnError(f"sing-box отверг конфиг: {out.strip()[:300]}")
        had_prev = os.path.exists(self.path)
        if had_prev:
            os.replace(self.path, self.path + ".prev")
        os.replace(new, self.path)
        code, out = await _run(self.cfg.apply_argv)
        if code == 0 and await self._watch():
            if had_prev:
                os.remove(self.path + ".prev")
            return "туннель поднят: панель и интернет доступны"
        reason = f"не запустился: {out.strip()[:200]}" if code else "нет связи через туннель"
        await self._rollback(had_prev)
        raise VpnError(f"{reason}; откат выполнен")

    async def _watch(self) -> bool:
        """Сторож: в течение rollback_seconds и панель, и интернет должны отвечать."""
        deadline = time.monotonic() + self.cfg.rollback_seconds
        while time.monotonic() < deadline:
            ok_panel = await asyncio.to_thread(self.panel_ok)
            ok_net = ok_panel and await asyncio.to_thread(self.internet_ok)
            if ok_panel and ok_net:
                return True
            await asyncio.sleep(self.poll)
        return False

    async def _rollback(self, had_prev: bool) -> None:
        if had_prev:
            os.replace(self.path + ".prev", self.path)
            await _run(self.cfg.apply_argv)
        else:
            os.remove(self.path)
            await _run(self.cfg.stop_argv)
