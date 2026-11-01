"""Соединение с одной панелью: hello → welcome, отправка выборок, приём действий.

Каждая панель — свой `PanelClient` со своим буфером: недоступность одной
панели не влияет на другие. Выборки лежат в буфере (deque с ограничением),
пока не отправлены, — так после обрыва досылаются последние N с исходными ts.
"""
from __future__ import annotations

import asyncio
import logging
import random
import ssl
from collections import deque
from typing import Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from . import __version__, guard, protocol
from .config import PanelConfig

log = logging.getLogger(__name__)

HelloFactory = Callable[[str], dict]
ActionHandler = Callable[[str, str], Awaitable[dict]]


class PanelRejected(Exception):
    """Панель ответила error (например, bad token)."""


def ssl_context(pcfg: PanelConfig) -> ssl.SSLContext | None:
    """Для wss:// — проверка сертификата всегда; свой CA через ca_file. ws:// — без TLS."""
    if not pcfg.url.startswith("wss://"):
        return None
    ctx = ssl.create_default_context(cafile=pcfg.ca_file) if pcfg.ca_file else ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class PanelClient:
    def __init__(self, pcfg: PanelConfig, hello_factory: HelloFactory, on_action: ActionHandler | None = None,
                 *, backoff_min: float = 1.0, backoff_max: float = 60.0, bad_token_max: float = 300.0,
                 welcome_timeout: float = 15.0, on_vpn: Callable[[dict], Awaitable[dict]] | None = None,
                 on_wol: Callable[[dict], Awaitable[dict]] | None = None,
                 on_update: Callable[["PanelClient", dict], Awaitable[None]] | None = None,
                 on_remote: Callable[["PanelClient", dict], Awaitable[None]] | None = None):
        self.cfg = pcfg
        self.name = pcfg.name
        self.hello_factory = hello_factory
        self.on_action = on_action
        self.on_vpn = on_vpn  # None — VPN на узле не включён, сообщения vpn игнорируются
        self.on_wol = on_wol  # None — [wol] не включён, сообщения wol игнорируются
        self.on_update = on_update  # None — [update] не включён, сообщения update игнорируются
        self.on_remote = on_remote  # config_get / config_set / uninstall; None — установка старой раскладкой
        self.backoff_min, self.backoff_max, self.bad_token_max = backoff_min, backoff_max, bad_token_max
        self.welcome_timeout = welcome_timeout
        self.buffer: deque[dict] = deque(maxlen=max(1, pcfg.buffer))  # выборки
        self.control: deque[dict] = deque(maxlen=100)  # action_result, event — вне очереди выборок
        self._wake: asyncio.Event | None = None
        self.connected = False
        self.interval_ms: int | None = None
        self.summary: dict | None = None
        self.last_error: str | None = None
        self._tasks: set[asyncio.Task] = set()

    # --- API для runner ---
    def publish(self, msg: dict) -> None:
        self.buffer.append(msg)
        self._poke()

    def send_control(self, msg: dict) -> None:
        self.control.append(msg)
        self._poke()

    def _poke(self):
        if self._wake is not None:
            self._wake.set()

    # --- цикл подключения ---
    async def run(self, stop: asyncio.Event) -> None:
        self._wake = asyncio.Event()
        delay = self.backoff_min
        while not stop.is_set():
            cap = self.backoff_max
            session = asyncio.create_task(self._session())
            stopper = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({session, stopper}, return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
            if session not in done:
                session.cancel()
                await asyncio.gather(session, return_exceptions=True)
                break
            exc = session.exception()
            self.connected = False
            if self.interval_ms is not None:
                delay = self.backoff_min  # сессия была рабочей (welcome) — переподключаемся быстро
            if exc is None:
                log.info("панель %s: соединение закрыто", self.name)
            elif isinstance(exc, PanelRejected):
                self.last_error = str(exc)
                log.warning("панель %s отказала: %s", self.name, exc)
                if "token" in str(exc):
                    cap = self.bad_token_max  # неверный токен — не долбим панель
            else:
                self.last_error = type(exc).__name__
                log.warning("панель %s: %s: %s", self.name, type(exc).__name__, exc)
            self.interval_ms = None
            wait = min(delay, cap) * random.uniform(0.8, 1.2)
            delay = min(delay * 2, cap)
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def _session(self) -> None:
        try:
            token = self.cfg.read_token()
        except OSError as exc:
            raise PanelRejected(f"не прочитать token_file: {exc.strerror}") from None
        async with connect(
            self.cfg.url,
            ssl=ssl_context(self.cfg),
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=1 << 20,
            user_agent_header=f"homelab-agent/{__version__}",
        ) as ws:
            await ws.send(protocol.encode(self.hello_factory(token)))
            del token
            await asyncio.wait_for(self._await_welcome(ws), timeout=self.welcome_timeout)
            self.connected = True
            self.last_error = None
            guard.write_mark(__version__)  # отметка для сторожа обновления: эта версия дошла до панели
            log.info("панель %s: подключено, буфер %d", self.name, len(self.buffer))
            sender = asyncio.create_task(self._sender(ws))
            try:
                async for raw in ws:
                    self._handle(raw)
                    if sender.done():  # ошибка отправки — завершаем сессию
                        break
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            if sender.done() and not sender.cancelled() and sender.exception():
                raise sender.exception()

    async def _await_welcome(self, ws) -> None:
        async for raw in ws:
            msg = protocol.decode(raw)
            if msg is None:
                continue
            if msg["type"] == "welcome":
                iv = msg.get("interval_ms")
                self.interval_ms = iv if isinstance(iv, int) else 0
                return
            if msg["type"] == "error":
                raise PanelRejected(str(msg.get("message", "error"))[:200])
        raise WebSocketException("соединение закрыто до welcome")

    async def _sender(self, ws) -> None:
        assert self._wake is not None
        while True:
            if self.control:
                msg = self.control[0]
                await ws.send(protocol.encode(msg))
                if self.control and self.control[0] is msg:
                    self.control.popleft()
            elif self.buffer:
                msg = self.buffer[0]
                await ws.send(protocol.encode(msg))
                # Убираем из буфера только после успешной отправки
                if self.buffer and self.buffer[0] is msg:
                    self.buffer.popleft()
            else:
                self._wake.clear()
                await self._wake.wait()

    def _handle(self, raw) -> None:
        msg = protocol.decode(raw)
        if msg is None:
            return
        kind = msg["type"]
        if kind == "action":
            aid, name = msg.get("id"), msg.get("name")
            if not isinstance(aid, str) or not isinstance(name, str) or len(aid) > 64 or len(name) > 64:
                return
            task = asyncio.create_task(self._run_action(aid, name))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif kind == "vpn" and self.on_vpn is not None:
            task = asyncio.create_task(self._run_vpn(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif kind == "wol" and self.on_wol is not None:
            task = asyncio.create_task(self._run_wol(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif kind == "update" and self.on_update is not None:
            task = asyncio.create_task(self.on_update(self, msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif kind in ("config_get", "config_set", "uninstall") and self.on_remote is not None:
            task = asyncio.create_task(self.on_remote(self, msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif kind == "summary":
            self.summary = {
                "online": msg.get("online") if isinstance(msg.get("online"), int) else None,
                "total": msg.get("total") if isinstance(msg.get("total"), int) else None,
                "alert": str(msg.get("alert") or "")[:120],
            }
        # welcome повторно, error после welcome и неизвестные типы — игнорируются

    async def flush(self, timeout: float = 5.0) -> None:
        """Дождаться, пока очередь служебных сообщений уйдёт (перед перезапуском агента)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self.control and self.connected and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)

    async def _run_vpn(self, msg: dict) -> None:
        self.send_control(await self.on_vpn(msg))

    async def _run_wol(self, msg: dict) -> None:
        self.send_control(await self.on_wol(msg))

    async def _run_action(self, aid: str, name: str) -> None:
        if self.on_action is None:
            result = protocol.action_result(aid, name, False, None, error="unknown action")
        else:
            result = await self.on_action(aid, name)
        self.send_control(result)
