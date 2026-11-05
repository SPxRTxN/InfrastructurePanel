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
import ipaddress
import os
import pwd
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .collectors.base import SAFE_ENV

log = logging.getLogger(__name__)

TOP_KEYS = {"log", "dns", "inbounds", "outbounds", "route"}
OUTBOUND_TYPES = {"vless", "vmess", "trojan", "shadowsocks", "hysteria2", "direct", "block", "dns"}
DNS_TYPES = {"tls", "local", "https", "udp", "tcp", "quic", "h3", "dhcp"}  # sing-box 1.12+: dns.servers[].type
SETUP_TIMEOUT = 300  # скачивание sing-box (~15 МБ) на слабом канале
XRAY_TOP_KEYS = {"log", "dns", "inbounds", "outbounds", "routing"}  # без api, stats, reverse, policy и пр.
XRAY_OUTBOUNDS = {"vless", "vmess", "trojan", "shadowsocks", "freedom", "blackhole"}
XRAY_PROXY_PROTOCOLS = {"vless", "vmess", "trojan", "shadowsocks"}
SB_UNIT, XRAY_UNIT = "homelab-vpn.service", "homelab-vpn-xray.service"
VPN_KEYS = {"enabled", "singbox", "setup", "config_dir", "apply_argv", "stop_argv", "check_url", "rollback_seconds",
            "xray", "xray_apply_argv", "xray_stop_argv", "xray_user"}



def _tail(out: str) -> str:
    """Причина отказа — в конце вывода (в начале у Xray баннер с версией), без многострочности."""
    return " ".join(out.split())[-300:]


# Должно совпадать со строками sudoers из install.sh
SETUP_RUNNER = ("systemd-run", "--wait", "--pipe", "--collect", "--quiet")


class VpnError(ValueError):
    pass


@dataclass
class VpnConfig:
    enabled: bool = False
    singbox: str = "/usr/local/bin/sing-box"
    setup: str = "/opt/homelab-agent/bin/vpn-setup"  # root-помощник: ставит sing-box (install.sh --vpn)
    config_dir: str = "/opt/homelab-agent/etc/vpn"  # = guard.ETC_DIR + "/vpn"
    apply_argv: list[str] = field(default_factory=lambda: ["sudo", "-n", "systemctl", "restart", "homelab-vpn.service"])
    stop_argv: list[str] = field(default_factory=lambda: ["sudo", "-n", "systemctl", "stop", "homelab-vpn.service"])
    xray: str = "/usr/local/bin/xray"  # второй движок; ставит `vpn-setup install-xray`
    xray_apply_argv: list[str] = field(default_factory=lambda: ["sudo", "-n", "systemctl", "restart", XRAY_UNIT])
    xray_stop_argv: list[str] = field(default_factory=lambda: ["sudo", "-n", "systemctl", "stop", XRAY_UNIT])
    xray_user: str = "hl-xray"  # его трафик исключается из TUN, иначе петля
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
    for key in ("apply_argv", "stop_argv", "xray_apply_argv", "xray_stop_argv"):
        val = getattr(cfg, key)
        if not isinstance(val, list) or not val or not all(isinstance(x, str) for x in val):
            raise ValueError(f"[vpn]: {key} — список аргументов")
    if not isinstance(cfg.setup, str) or not cfg.setup.startswith("/"):
        raise ValueError("[vpn]: setup — абсолютный путь")
    if not isinstance(cfg.rollback_seconds, int) or not 10 <= cfg.rollback_seconds <= 600:
        raise ValueError("[vpn]: rollback_seconds — 10..600")
    return cfg


# ---------- Проверка присланного конфига ----------

def _resolve(host: str) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})
    except OSError:
        return []


def prepare(config: dict, panel_urls: list[str], resolve=_resolve, extra_types: frozenset = frozenset()) -> dict:
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
        if not isinstance(ob, dict) or ob.get("type") not in OUTBOUND_TYPES | extra_types:
            raise VpnError(f"тип outbound {ob.get('type') if isinstance(ob, dict) else ob!r} не разрешён")
    _check_dns_route(config)
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


def _check_dns_route(config: dict) -> None:
    """Формат sing-box 1.12+: dns.servers[] с type/server, route.default_domain_resolver. Старый (address) тоже ок."""
    dns = config.get("dns")
    if dns is not None:
        servers = dns.get("servers", []) if isinstance(dns, dict) else None
        if not isinstance(servers, list) or not all(isinstance(x, dict) for x in servers):
            raise VpnError("dns.servers — список объектов")
        for sv in servers:
            if "type" in sv and sv["type"] not in DNS_TYPES:
                raise VpnError(f"тип DNS-сервера {sv['type']!r} не разрешён")
    route = config.get("route")
    if route is not None:
        if not isinstance(route, dict):
            raise VpnError("route — объект")
        if not isinstance(route.get("default_domain_resolver", ""), (str, dict)):
            raise VpnError("route.default_domain_resolver — строка (tag DNS-сервера)")


def singbox_version(path: str) -> str:
    """Версия установленного sing-box или '' (нет файла/не запускается)."""
    if not os.path.isfile(path):
        return ""
    try:
        out = subprocess.run([path, "version"], capture_output=True, text=True, timeout=5, env=SAFE_ENV,
                             stdin=subprocess.DEVNULL, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"version\s+(\S+)", out)
    return m.group(1) if m else ""


def xray_version(path: str) -> str:
    """Версия установленного Xray или '' (`xray version` печатает «Xray 1.x.y (…)»)."""
    if not os.path.isfile(path):
        return ""
    try:
        out = subprocess.run([path, "version"], capture_output=True, text=True, timeout=5, env=SAFE_ENV,
                             stdin=subprocess.DEVNULL, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"Xray\s+(\S+)", out)
    return m.group(1) if m else ""


def info(cfg: VpnConfig) -> dict:
    """info.vpn для hello: версии движков и есть ли помощник установки."""
    return {"singbox": singbox_version(cfg.singbox), "xray": xray_version(cfg.xray),
            "setup": os.path.isfile(cfg.setup)}


# ---------- Проверка конфига Xray ----------

def _proxy_entries(ob: dict) -> list[dict]:
    """Записи с адресом сервера в outbound Xray: плоский формат (settings.address), vnext (vless/vmess), servers."""
    st = ob.get("settings")
    if not isinstance(st, dict):
        return []
    found = [st] if "address" in st else []
    for key in ("vnext", "servers"):
        found += [e for e in st.get(key, []) if isinstance(e, dict) and "address" in e]
    return found


def prepare_xray(xray: dict, resolve=_resolve) -> tuple[dict, int, list[str]]:
    """Проверить конфиг Xray по белому списку и подставить IP вместо имени VPN-сервера (до подъёма туннеля).
    Возвращает (конфиг, порт локального SOCKS, IP сервера). Имя сервера остаётся в serverName TLS/Reality."""
    if not isinstance(xray, dict) or set(xray) - XRAY_TOP_KEYS:
        raise VpnError("в конфиге Xray лишние разделы (разрешены: " + ", ".join(sorted(XRAY_TOP_KEYS)) + ")")
    inbounds = xray.get("inbounds")
    if not isinstance(inbounds, list) or len(inbounds) != 1 or not isinstance(inbounds[0], dict) \
            or inbounds[0].get("protocol") != "socks" or inbounds[0].get("listen") != "127.0.0.1" \
            or not isinstance(inbounds[0].get("port"), int):
        raise VpnError("Xray: ожидается ровно один inbound socks на 127.0.0.1")
    outbounds = xray.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds:
        raise VpnError("Xray: нет outbounds")
    for ob in outbounds:
        if not isinstance(ob, dict) or ob.get("protocol") not in XRAY_OUTBOUNDS:
            raise VpnError(f"Xray: протокол outbound {ob.get('protocol') if isinstance(ob, dict) else ob!r} не разрешён")
    out = json.loads(json.dumps(xray))
    ips: list[str] = []
    proxies = [ob for ob in out["outbounds"] if ob["protocol"] in XRAY_PROXY_PROTOCOLS]
    if not proxies:
        raise VpnError("Xray: нет outbound с VPN-сервером")
    for ob in proxies:
        for entry in _proxy_entries(ob):
            host = str(entry.get("address", ""))
            try:
                ipaddress.ip_address(host)
                ip = host
            except ValueError:
                found = resolve(host)
                if not found:
                    raise VpnError(f"Xray: не удалось определить IP сервера {host}") from None
                ip = next((x for x in found if ":" not in x), found[0])
                # TLS/Reality без своего serverName берут его из адреса — сохраняем имя
                ss = ob.setdefault("streamSettings", {})
                for sec in ("tlsSettings", "realitySettings"):
                    if isinstance(ss.get(sec), dict):
                        ss[sec].setdefault("serverName", host)
            entry["address"] = ip
            if ip not in ips:
                ips.append(ip)
    if not ips:
        raise VpnError("Xray: в outbound нет адреса сервера")
    return out, inbounds[0]["port"], ips


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


def _tunnel_up(iface: str, services: tuple = (SB_UNIT,)) -> bool:
    """sing-box действительно работает: служба активна и TUN-интерфейс есть. Без этого панель и интернет
    доступны и мимо туннеля — сторож засчитал бы упавший sing-box как успех."""
    if iface and not os.path.exists(f"/sys/class/net/{iface}"):
        return False
    try:  # все службы движка должны быть активны (для Xray — и xray, и TUN sing-box)
        return all(subprocess.run(["systemctl", "is-active", "--quiet", unit], timeout=5,
                                  env=SAFE_ENV).returncode == 0  # nosec B603 B607 — фиксированная команда
                   for unit in services)
    except (OSError, subprocess.TimeoutExpired):
        return False


def _uid_of(user: str) -> int:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        raise VpnError(f"нет системного пользователя {user} (install.sh --vpn создаёт его)") from None


class VpnManager:
    def __init__(self, cfg: VpnConfig, panel_urls: list[str], *, resolve=_resolve, panel_ok=None, internet_ok=None,
                 tunnel_up=None, poll: float = 3.0, uid_of=_uid_of):
        self.cfg = cfg
        self.panel_urls = panel_urls
        self.resolve = resolve
        self.panel_ok = panel_ok or (lambda: any(_tcp_ok(u) for u in self.panel_urls))
        self.internet_ok = internet_ok or (lambda: _http_ok(self.cfg.check_url))
        self.tunnel_up = tunnel_up or (lambda iface: _tunnel_up(iface, self.services))
        self.uid_of = uid_of
        from .sysinfo import interfaces
        self.ifaces = interfaces  # тесты подменяют: реальный список — с psutil
        self.touched = False
        self.iface = ""
        self.services: tuple = (SB_UNIT,)
        self.poll = poll
        self._lock = asyncio.Lock()

    @property
    def path(self) -> str:
        return os.path.join(self.cfg.config_dir, "config.json")

    @property
    def xpath(self) -> str:
        return os.path.join(self.cfg.config_dir, "xray.json")

    async def handle(self, msg: dict) -> dict:
        vid = str(msg.get("id", ""))[:64]
        if self._lock.locked():
            return {"type": "vpn_result", "id": vid, "ok": False, "detail": "уже применяется другой конфиг"}
        async with self._lock:
            try:
                detail, engine = await (self._off() if msg.get("mode") == "off" else self._apply(msg))
                res = {"type": "vpn_result", "id": vid, "ok": True, "detail": detail, "engine": engine}
            except VpnError as exc:
                log.warning("vpn: %s", exc)
                res = {"type": "vpn_result", "id": vid, "ok": False, "detail": str(exc)[:500], "engine": ""}
        # Интерфейсы после смены: в панели сразу виден TUN (hello с интерфейсами — только при подключении)
        res["interfaces"] = await asyncio.to_thread(self.ifaces)
        return res

    async def _off(self) -> tuple[str, str]:
        code, out = await _run(self.cfg.stop_argv)
        if code:
            raise VpnError(f"не удалось остановить: {out.strip()[:200]}")
        await _run(self.cfg.xray_stop_argv)
        for p in (self.path, self.xpath):
            for f in (p, p + ".prev"):
                if os.path.exists(f):
                    os.remove(f)
        return "VPN выключен, трафик напрямую", ""

    async def _apply(self, msg: dict) -> tuple[str, str]:
        """Движки по очереди: sing-box (config), затем Xray (xray). Ни один — возврат прежнего состояния."""
        singbox, xray = msg.get("config"), msg.get("xray")
        os.makedirs(self.cfg.config_dir, mode=0o700, exist_ok=True)
        prev = {p: os.path.exists(p) for p in (self.path, self.xpath)}
        for p, had in prev.items():
            if had:
                shutil.copyfile(p, p + ".prev")
                os.chmod(p + ".prev", 0o600)
        self.touched = False
        reasons = []
        if singbox is None:
            reasons.append("sing-box: сервер не поддерживается этим движком")
        else:
            why = await self._try_singbox(singbox, prev[self.xpath])
            if not why:
                await self._finish_singbox(prev)
                return "туннель поднят: панель и интернет доступны", "sing-box"
            reasons.append(f"sing-box: {why}")
        if xray is None:
            reasons.append("xray: сервер не поддерживается этим движком")
        else:
            why = await self._try_xray(xray)
            if not why:
                self._drop_prev()
                head = reasons[0] + "; " if singbox is not None else ""
                return f"{head}туннель поднят через Xray: панель и интернет доступны", "xray"
            reasons.append(f"xray: {why}")
        await self._restore(prev)
        raise VpnError("; ".join(reasons) + "; откат выполнен")

    def _drop_prev(self) -> None:
        for p in (self.path, self.xpath):
            if os.path.exists(p + ".prev"):
                os.remove(p + ".prev")

    async def _finish_singbox(self, prev: dict) -> None:
        """Работает sing-box: служба Xray остановлена, её конфиг (ключи сервера) не хранится."""
        if prev[self.xpath]:
            await _run(self.cfg.xray_stop_argv)
        if os.path.exists(self.xpath):
            os.remove(self.xpath)
        self._drop_prev()

    def _write(self, path: str, data: dict) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # в конфиге ключи сервера
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)

    async def _try_singbox(self, config, had_xray: bool) -> str:
        """Пусто — поднялся, иначе причина (службы остановлены, новые файлы убраны)."""
        new = self.path + ".new"
        try:
            final = prepare(config, self.panel_urls, self.resolve)
            self.iface = str(final["inbounds"][0].get("interface_name", ""))
            await self._ensure(self.cfg.singbox, "install", "sing-box")
            self._write(new, final)
            code, out = await _run([self.cfg.singbox, "check", "-c", new])
            if code:
                os.remove(new)
                raise VpnError(f"sing-box отверг конфиг: {out.strip()[:300]}")
        except VpnError as exc:
            return str(exc)
        os.replace(new, self.path)
        self.services = (SB_UNIT,)
        self.touched = True
        if had_xray:  # прежний движок — Xray: его служба не должна работать, пока проверяем sing-box
            await _run(self.cfg.xray_stop_argv)
        code, out = await _run(self.cfg.apply_argv)
        failure = await self._watch() if code == 0 else ""
        if code == 0 and not failure:
            return ""
        await self._stop_all()
        return f"не запустился: {out.strip()[:200]}" if code else failure

    async def _try_xray(self, xray) -> str:
        # Xray выбирает формат по расширению: «xray.json.new» он не читает («failed to load config files»)
        xnew, snew = self.xpath[: -len(".json")] + ".new.json", self.path + ".new"
        try:
            if not isinstance(xray, dict):
                raise VpnError("Xray: ожидается объект {singbox, xray}")
            conf, port, ips = prepare_xray(xray.get("xray"), self.resolve)
            tun = prepare(xray.get("singbox"), self.panel_urls, self.resolve, frozenset({"socks"}))
            socks = [o for o in tun["outbounds"] if o.get("type") == "socks"]
            if not socks or any(o.get("server") != "127.0.0.1" or o.get("server_port") != port for o in socks):
                raise VpnError("Xray: outbound socks sing-box должен вести на 127.0.0.1 и порт inbound Xray")
            inb = tun["inbounds"][0]
            inb["exclude_uid"] = sorted({*inb.get("exclude_uid", []), self.uid_of(self.cfg.xray_user)})
            exclude = list(inb["route_exclude_address"])
            for ip in ips:
                cidr = f"{ip}/32" if ":" not in ip else f"{ip}/128"
                if cidr not in exclude:
                    exclude.append(cidr)
            inb["route_exclude_address"] = exclude
            self.iface = str(inb.get("interface_name", ""))
            await self._ensure(self.cfg.xray, "install-xray", "Xray")
            self._write(xnew, conf)
            self._write(snew, tun)
            code, out = await _run([self.cfg.xray, "run", "-test", "-c", xnew])
            if code:
                out = f"Xray отверг конфиг: {_tail(out)}"
            else:
                code, out = await _run([self.cfg.singbox, "check", "-c", snew])
                out = f"sing-box отверг конфиг: {_tail(out)}"
            if code:
                for f in (xnew, snew):
                    os.remove(f)
                raise VpnError(out[:300])
        except VpnError as exc:
            return str(exc)
        os.replace(xnew, self.xpath)
        os.replace(snew, self.path)
        self.services = (XRAY_UNIT, SB_UNIT)
        self.touched = True
        code, out = await _run(self.cfg.xray_apply_argv)  # Xray первым: TUN сразу получает рабочий SOCKS
        if code == 0:
            code, out = await _run(self.cfg.apply_argv)
        failure = await self._watch() if code == 0 else ""
        if code == 0 and not failure:
            return ""
        await self._stop_all()
        return f"не запустился: {out.strip()[:200]}" if code else failure

    async def _stop_all(self) -> None:
        await _run(self.cfg.stop_argv)
        await _run(self.cfg.xray_stop_argv)

    async def _ensure(self, path: str, arg: str, name: str) -> None:
        """Нет движка — просим root-помощника поставить (sudo -n, строки в sudoers ставит install.sh --vpn).
        Через systemd-run: помощник запускается отдельной службой вне песочницы агента — в ней (ProtectSystem=strict)
        /usr/local/bin только для чтения даже для root, и установка падала с «не удалось записать»."""
        if os.path.exists(path):
            return
        log.info("vpn: %s не найден, запускаю %s %s", name, self.cfg.setup, arg)
        code, out = await _run(["sudo", "-n", *SETUP_RUNNER, self.cfg.setup, arg], SETUP_TIMEOUT)
        if code or not os.path.exists(path):
            raise VpnError(f"не удалось установить {name} (код {code}): {out.strip()[-300:]}")

    async def _watch(self) -> str:
        """Сторож: за rollback_seconds движок должен работать, а панель и интернет — отвечать, дважды подряд
        (служба с Restart=on-failure между падениями ненадолго бывает активной). Пусто — успех, иначе причина."""
        deadline = time.monotonic() + self.cfg.rollback_seconds
        reason, streak = "нет связи через туннель", 0
        while time.monotonic() < deadline:
            if not await asyncio.to_thread(self.tunnel_up, self.iface):
                reason, streak = "sing-box не работает (sudo journalctl -u homelab-vpn)", 0
            elif not await asyncio.to_thread(self.panel_ok):
                reason, streak = "панель недоступна через туннель", 0
            elif not await asyncio.to_thread(self.internet_ok):
                reason, streak = "нет интернета через туннель", 0
            else:
                streak += 1
                if streak >= 2:
                    return ""
            await asyncio.sleep(self.poll)
        return reason

    async def _restore(self, prev: dict) -> None:
        """Все движки отказали: вернуть прежние файлы и прежний движок, а если прежнего не было — всё остановить."""
        for p, had in prev.items():
            if had:
                os.replace(p + ".prev", p)
            elif os.path.exists(p):
                os.remove(p)
        if not self.touched:
            return  # службы не трогали — работают как раньше
        if prev[self.xpath]:
            await _run(self.cfg.xray_apply_argv)
            await _run(self.cfg.apply_argv)
        elif prev[self.path]:
            await _run(self.cfg.xray_stop_argv)
            await _run(self.cfg.apply_argv)
        else:
            await self._stop_all()
