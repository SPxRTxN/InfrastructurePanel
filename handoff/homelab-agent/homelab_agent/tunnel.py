"""TCP-туннель панель -> агент -> локальный sshd узла (SSH-консоль и передача файлов через агента).

Агент только пересылает байты на ОДИН адрес из своего конфига (`[terminal] target`, строго loopback); SSH
(вход, ключ хоста, PTY, SFTP) делает панель поверх туннеля. Панель не присылает адрес и команды: в tunnel_open
есть лишь id. Содержимое трафика не читается и не логируется - только открыл/закрыл и число байт.

Изоляция от хоста и телеметрии: у каждого сеанса свои задачи asyncio; очередь на запись в сокет ограничена
(переполнение - сеанс закрывается); отправка в панель идёт через отдельную очередь клиента (`send_tunnel`),
которую отправитель чередует с выборками; обрыв связи с панелью закрывает все её сеансы.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

TERMINAL_KEYS = {"enabled", "target", "max_sessions", "rate_kbps", "idle_timeout"}
CHUNK = 32 * 1024  # максимум полезных байт в одном tunnel_data
QUEUE_LIMIT = 256 * 1024  # байт панель -> сокет на сеанс; больше - сеанс закрывается
CONNECT_TIMEOUT = 5.0


@dataclass
class TunnelConfig:
    enabled: bool = False
    target: str = "127.0.0.1:22"
    max_sessions: int = 2
    rate_kbps: int = 4096  # килобит/с на все сеансы вместе, в обе стороны
    idle_timeout: float = 900


def split_target(target) -> tuple[str, int]:
    """'127.0.0.1:22' или '[::1]:22' -> (host, port); не loopback - ValueError."""
    if not isinstance(target, str) or ":" not in target:
        raise ValueError("[terminal]: target - строка вида \"127.0.0.1:22\"")
    host, _, port_s = target.rpartition(":")
    host = host.strip("[]")
    try:
        ip = ipaddress.ip_address(host)
        port = int(port_s)
    except ValueError:
        raise ValueError("[terminal]: target - IP-адрес loopback и порт, например \"127.0.0.1:22\"") from None
    if not ip.is_loopback:
        raise ValueError("[terminal]: target должен быть loopback (127.0.0.0/8 или ::1): "
                         "агент не пересылает трафик на другие адреса")
    if not 1 <= port <= 65535:
        raise ValueError("[terminal]: порт target - 1..65535")
    return str(ip), port


def parse_terminal(raw: dict | None) -> TunnelConfig:
    raw = dict(raw or {})
    unknown = set(raw) - TERMINAL_KEYS
    if unknown:
        raise ValueError(f"[terminal]: неизвестные ключи: {', '.join(sorted(unknown))}")
    cfg = TunnelConfig(**raw)
    if not isinstance(cfg.enabled, bool):
        raise ValueError("[terminal]: enabled - true/false")
    split_target(cfg.target)
    for key, lo, hi in (("max_sessions", 1, 8), ("rate_kbps", 64, 1_000_000), ("idle_timeout", 10, 86400)):
        v = getattr(cfg, key)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"[terminal]: {key} - целое {lo}..{hi}")
    return cfg


class RateLimiter:
    """Общий лимит байт/с (token bucket): вызывающий ждёт, если вышел за лимит."""

    def __init__(self, bytes_per_s: float):
        self.rate = bytes_per_s
        self.cap = max(bytes_per_s, CHUNK)
        self.tokens = self.cap
        self.last = time.monotonic()

    async def take(self, n: int) -> None:
        now = time.monotonic()
        self.tokens = min(self.cap, self.tokens + (now - self.last) * self.rate)
        self.last = now
        self.tokens -= n
        if self.tokens < 0:
            await asyncio.sleep(-self.tokens / self.rate)


class Session:
    def __init__(self, mgr: "TunnelManager", client, tid: str):
        self.mgr, self.client, self.tid = mgr, client, tid
        self.inq: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.queued = 0
        self.up = self.down = 0  # панель -> сокет, сокет -> панель
        self.last = time.monotonic()
        self.closed = False
        self.reason = ""
        self.writer: asyncio.StreamWriter | None = None
        self.tasks: list[asyncio.Task] = []

    def push(self, data: bytes) -> None:
        if self.closed:
            return
        if self.queued + len(data) > QUEUE_LIMIT:
            self.close("queue overflow")
            return
        self.queued += len(data)
        self.inq.put_nowait(data)

    def close(self, reason: str, notify: bool = True) -> None:
        """Идемпотентно; можно вызывать из любой задачи сеанса."""
        if self.closed:
            return
        self.closed, self.reason = True, reason
        self.mgr.sessions.pop((id(self.client), self.tid), None)
        if notify:
            self.client.put_tunnel({"type": "tunnel_close", "id": self.tid, "reason": reason[:100]})
        if self.writer is not None:
            self.writer.close()
        me = asyncio.current_task()
        for t in self.tasks:
            if t is not me:
                t.cancel()
        log.info("tunnel %s закрыт (%s): в сокет %d Б, из сокета %d Б", self.tid, reason, self.up, self.down)

    async def run(self) -> None:
        host, port = split_target(self.mgr.cfg.target)
        try:
            reader, self.writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            self.closed = True
            self.mgr.sessions.pop((id(self.client), self.tid), None)
            self.client.put_tunnel({"type": "tunnel_opened", "id": self.tid, "ok": False, "detail": "connect failed"})
            return
        if self.closed:  # панель закрыла, пока подключались
            self.writer.close()
            return
        log.info("tunnel %s открыт", self.tid)
        self.client.put_tunnel({"type": "tunnel_opened", "id": self.tid, "ok": True})
        self.tasks = [asyncio.create_task(self._to_panel(reader)), asyncio.create_task(self._to_socket()),
                      asyncio.create_task(self._watch())]
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _to_panel(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.read(CHUNK)
                if not data:
                    self.close("closed by host")
                    return
                await self.mgr.rate.take(len(data))
                self.down += len(data)
                self.last = time.monotonic()
                await self.client.send_tunnel({"type": "tunnel_data", "id": self.tid,
                                               "d": base64.b64encode(data).decode()})
        except OSError:
            self.close("socket error")

    async def _to_socket(self) -> None:
        try:
            while True:
                data = await self.inq.get()
                self.queued -= len(data)
                await self.mgr.rate.take(len(data))
                self.writer.write(data)
                await self.writer.drain()
                self.up += len(data)
                self.last = time.monotonic()
        except OSError:
            self.close("socket error")

    async def _watch(self) -> None:
        idle = self.mgr.cfg.idle_timeout
        while True:
            await asyncio.sleep(max(0.05, min(5.0, idle / 4)))
            if time.monotonic() - self.last > idle:
                self.close("idle timeout")
                return


class TunnelManager:
    def __init__(self, cfg: TunnelConfig):
        self.cfg = cfg
        self.rate = RateLimiter(cfg.rate_kbps * 125)  # килобит/с -> байт/с
        self.sessions: dict[tuple[int, str], Session] = {}
        self._tasks: set[asyncio.Task] = set()

    def handle(self, client, msg: dict) -> None:
        """tunnel_open / tunnel_data / tunnel_close от панели. Не блокирует разбор входящих."""
        kind = msg.get("type")
        tid = msg.get("id")
        if not isinstance(tid, str) or not 0 < len(tid) <= 32:
            return
        key = (id(client), tid)
        if kind == "tunnel_open":
            if key in self.sessions:
                return
            if len(self.sessions) >= self.cfg.max_sessions:
                client.put_tunnel({"type": "tunnel_opened", "id": tid, "ok": False, "detail": "busy"})
                return
            sess = Session(self, client, tid)
            self.sessions[key] = sess
            task = asyncio.create_task(sess.run())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        sess = self.sessions.get(key)
        if sess is None:  # неизвестный id - игнор
            return
        if kind == "tunnel_close":
            sess.close(str(msg.get("reason") or "closed by panel")[:100], notify=False)
        elif kind == "tunnel_data":
            try:
                data = base64.b64decode(msg.get("d") or "", validate=True)
            except (binascii.Error, ValueError, TypeError):
                sess.close("bad data")
                return
            if len(data) > CHUNK:
                sess.close("chunk too large")
                return
            sess.last = time.monotonic()
            sess.push(data)

    def gone(self, client) -> None:
        """Связь с панелью оборвалась: все её сеансы закрываются (сообщать некому)."""
        for sess in [s for s in self.sessions.values() if s.client is client]:
            sess.close("panel disconnected", notify=False)
