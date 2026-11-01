"""Туннель к локальному sshd: фейковый TCP-сервер на 127.0.0.1 (порт 0), без сети наружу."""
import asyncio
import base64
import time

import pytest

from homelab_agent.config import parse_config, check_config
from homelab_agent.remote_config import MANAGED, LOCAL_ONLY
from homelab_agent.runner import Agent
from homelab_agent.transport import PanelClient
from homelab_agent.tunnel import (CHUNK, QUEUE_LIMIT, TunnelConfig, TunnelManager, parse_terminal, split_target)


class FakeClient:
    """Достаточно для менеджера: сообщения к панели собираются в список."""

    def __init__(self):
        self.out: list[dict] = []

    def put_tunnel(self, msg):
        self.out.append(msg)

    async def send_tunnel(self, msg):
        self.out.append(msg)

    async def wait(self, kind, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in self.out:
                if m["type"] == kind:
                    self.out.remove(m)
                    return m
            await asyncio.sleep(0.01)
        raise AssertionError(f"нет {kind}: {self.out}")

    def data(self) -> bytes:
        return b"".join(base64.b64decode(m["d"]) for m in self.out if m["type"] == "tunnel_data")


async def echo_server(received: list | None = None, hold: bool = False):
    async def handle(r, w):
        while data := await r.read(65536):
            if received is not None:
                received.append(data)
            if not hold:
                w.write(data)
                await w.drain()
        w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


def cfg_for(port, **kw):
    kw.setdefault("rate_kbps", 100_000)
    return TunnelConfig(enabled=True, target=f"127.0.0.1:{port}", **kw)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_echo_both_ways_and_close():
    async def main():
        srv, port = await echo_server()
        mgr, cl = TunnelManager(cfg_for(port)), FakeClient()
        mgr.handle(cl, {"type": "tunnel_open", "id": "t-1"})
        assert (await cl.wait("tunnel_opened"))["ok"] is True
        mgr.handle(cl, {"type": "tunnel_data", "id": "t-1", "d": b64(b"hello")})
        await asyncio.sleep(0.2)
        assert cl.data() == b"hello"
        mgr.handle(cl, {"type": "tunnel_close", "id": "t-1"})
        await asyncio.sleep(0.1)
        assert not mgr.sessions
        mgr.handle(cl, {"type": "tunnel_data", "id": "t-1", "d": b64(b"x")})  # неизвестный id - игнор
        srv.close()

    asyncio.run(main())


def test_big_transfer_chunks_limit():
    async def main():
        got: list[bytes] = []
        srv, port = await echo_server(got, hold=True)
        mgr, cl = TunnelManager(cfg_for(port)), FakeClient()
        mgr.handle(cl, {"type": "tunnel_open", "id": "t-1"})
        await cl.wait("tunnel_opened")
        blob = bytes(range(256)) * 400  # 100 КБ тремя кусками по <= 32 КБ
        for i in range(0, len(blob), CHUNK):
            mgr.handle(cl, {"type": "tunnel_data", "id": "t-1", "d": b64(blob[i:i + CHUNK])})
        await asyncio.sleep(0.3)
        assert b"".join(got) == blob
        mgr.handle(cl, {"type": "tunnel_data", "id": "t-1", "d": b64(b"a" * (CHUNK + 1))})
        assert (await cl.wait("tunnel_close"))["reason"] == "chunk too large"
        srv.close()

    asyncio.run(main())


def test_busy_and_connect_failed():
    async def main():
        srv, port = await echo_server()
        mgr, cl = TunnelManager(cfg_for(port, max_sessions=1)), FakeClient()
        mgr.handle(cl, {"type": "tunnel_open", "id": "t-1"})
        mgr.handle(cl, {"type": "tunnel_open", "id": "t-2"})
        busy = await cl.wait("tunnel_opened")
        assert busy["id"] == "t-2" and busy["ok"] is False and busy["detail"] == "busy"
        assert (await cl.wait("tunnel_opened"))["id"] == "t-1"
        srv.close()  # слушающий сокет закрыт сразу (wait_closed ждал бы живые соединения)
        mgr2, cl2 = TunnelManager(cfg_for(port)), FakeClient()  # порт уже закрыт
        mgr2.handle(cl2, {"type": "tunnel_open", "id": "t-9"})
        res = await cl2.wait("tunnel_opened")
        assert res["ok"] is False and res["detail"] == "connect failed" and not mgr2.sessions
        mgr.gone(cl)

    asyncio.run(main())


def test_idle_timeout():
    async def main():
        srv, port = await echo_server()
        mgr, cl = TunnelManager(cfg_for(port, idle_timeout=0.3)), FakeClient()
        mgr.handle(cl, {"type": "tunnel_open", "id": "t-1"})
        await cl.wait("tunnel_opened")
        assert (await cl.wait("tunnel_close"))["reason"] == "idle timeout"
        assert not mgr.sessions
        srv.close()

    asyncio.run(main())


def test_panel_disconnect_closes_all_and_host_close_notifies():
    async def main():
        srv, port = await echo_server()
        mgr, cl = TunnelManager(cfg_for(port)), FakeClient()
        for t in ("t-1", "t-2"):
            mgr.handle(cl, {"type": "tunnel_open", "id": t})
            await cl.wait("tunnel_opened")
        assert len(mgr.sessions) == 2
        mgr.gone(cl)
        assert not mgr.sessions
        srv.close()
        # сервер закрыл соединение сам - панель получает tunnel_close
        async def closer(r, w):
            w.close()
        s2 = await asyncio.start_server(closer, "127.0.0.1", 0)
        mgr2, cl2 = TunnelManager(cfg_for(s2.sockets[0].getsockname()[1])), FakeClient()
        mgr2.handle(cl2, {"type": "tunnel_open", "id": "t-1"})
        await cl2.wait("tunnel_opened")
        assert (await cl2.wait("tunnel_close"))["reason"] == "closed by host"
        s2.close()

    asyncio.run(main())


def test_queue_overflow_closes_session():
    async def main():
        srv, port = await echo_server(hold=True)
        # очень медленный лимит: очередь панель -> сокет копится и переполняется
        mgr, cl = TunnelManager(cfg_for(port, rate_kbps=64)), FakeClient()
        mgr.handle(cl, {"type": "tunnel_open", "id": "t-1"})
        await cl.wait("tunnel_opened")
        for _ in range(QUEUE_LIMIT // CHUNK + 3):
            mgr.handle(cl, {"type": "tunnel_data", "id": "t-1", "d": b64(b"z" * CHUNK)})
        assert (await cl.wait("tunnel_close"))["reason"] == "queue overflow"
        srv.close()

    asyncio.run(main())


def test_rate_limit_slows_transfer():
    async def main():
        srv, port = await echo_server(hold=True)
        mgr, cl = TunnelManager(cfg_for(port, rate_kbps=64)), FakeClient()  # 8 КБ/с, ёмкость 32 КБ
        mgr.handle(cl, {"type": "tunnel_open", "id": "t-1"})
        await cl.wait("tunnel_opened")
        start = time.monotonic()
        for _ in range(3):
            mgr.handle(cl, {"type": "tunnel_data", "id": "t-1", "d": b64(b"z" * CHUNK)})
        await asyncio.sleep(1.0)
        # за секунду при 8 КБ/с (+32 КБ запаса) в сокет не может уйти все 96 КБ
        assert next(iter(mgr.sessions.values())).up < 3 * CHUNK
        assert time.monotonic() - start >= 1.0
        mgr.gone(cl)
        srv.close()

    asyncio.run(main())


def test_config_validation():
    assert parse_terminal(None).enabled is False
    assert split_target("[::1]:2222") == ("::1", 2222)
    assert split_target("127.0.0.5:22") == ("127.0.0.5", 22)
    for bad in ("192.168.1.5:22", "localhost:22", "0.0.0.0:22", "127.0.0.1", "127.0.0.1:0"):
        with pytest.raises(ValueError):
            parse_terminal({"enabled": True, "target": bad})
    for key, val in (("max_sessions", 0), ("max_sessions", 9), ("rate_kbps", 1), ("idle_timeout", 1),
                     ("enabled", "yes"), ("bogus", 1)):
        with pytest.raises(ValueError):
            parse_terminal({key: val})
    cfg = parse_config({"node": "pi", "terminal": {"enabled": True, "target": "10.0.0.1:22"}})
    assert any("loopback" in p for p in check_config(cfg))


def test_hello_features_and_local_only():
    off = Agent(parse_config({"node": "pi"}), collectors=[])
    assert "tunnel" not in off.hello("t")["features"] and "tunnel_max" not in off.hello("t")
    on = Agent(parse_config({"node": "pi", "terminal": {"enabled": True, "max_sessions": 3}}), collectors=[])
    msg = on.hello("t")
    assert "tunnel" in msg["features"] and msg["tunnel_max"] == 3
    assert "terminal" in LOCAL_ONLY and "terminal" not in MANAGED


def test_disabled_agent_answers_tunnel_open():
    from homelab_agent.config import PanelConfig

    cl = PanelClient(PanelConfig(url="wss://x/agent/ws", token="t"), lambda t: {})
    cl._handle('{"type":"tunnel_open","id":"t-1"}')
    assert cl.control[0] == {"type": "tunnel_opened", "id": "t-1", "ok": False, "detail": "disabled"}
    cl._handle('{"type":"tunnel_data","id":"t-1","d":""}')  # прочее молча игнорируется
    assert len(cl.control) == 1
