"""Фейковая панель: локальный WebSocket-сервер на 127.0.0.1 со случайным портом."""
from __future__ import annotations

import asyncio
import json

from websockets.asyncio.server import serve


class FakePanel:
    def __init__(self, token: str = "good-token", interval_ms: int = 10000):
        self.token = token
        self.interval_ms = interval_ms
        self.hellos: list[dict] = []
        self.messages: list[dict] = []
        self.connections = []
        self.server = None
        self.port = 0
        self.on_hello = None  # колбэк(ws) после welcome — для отправки сообщений агенту

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/agent/ws"

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m.get("type") == kind]

    async def handler(self, ws):
        hello = json.loads(await ws.recv())
        self.hellos.append(hello)
        if hello.get("token") != self.token:
            await ws.send(json.dumps({"type": "error", "message": "bad token"}))
            await ws.close()
            return
        await ws.send(json.dumps({"type": "welcome", "interval_ms": self.interval_ms}))
        self.connections.append(ws)
        if self.on_hello:
            await self.on_hello(ws)
        async for raw in ws:
            self.messages.append(json.loads(raw))

    async def start(self, port: int = 0):
        self.server = await serve(self.handler, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def wait(self, predicate, timeout: float = 5.0):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.02)

        await asyncio.wait_for(poll(), timeout)
