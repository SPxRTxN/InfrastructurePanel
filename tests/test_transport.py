import asyncio
import json
import socket
import ssl

import pytest

from fakepanel import FakePanel
from homelab_agent import protocol
from homelab_agent.collectors import Collector, Sensor
from homelab_agent.config import PanelConfig, parse_config
from homelab_agent.runner import Agent
from homelab_agent.transport import PanelClient, ssl_context

FAST = {"backoff_min": 0.05, "backoff_max": 0.2, "bad_token_max": 0.3, "welcome_timeout": 2}


class ConstCollector(Collector):
    name = "const"

    def sensors(self):
        return [Sensor("test.value", "state", "Тест")]

    def read(self):
        return {"test.value": 42}


def panel_cfg(url, token_file=None, token=None, buffer=360):
    return PanelConfig(url=url, token_file=token_file, token=token, insecure=True, buffer=buffer, name="test")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_protocol_decode_ignores_garbage():
    assert protocol.decode("not json") is None
    assert protocol.decode("[1,2]") is None
    assert protocol.decode('{"no": "type"}') is None
    assert protocol.decode(b'{"type": "x", "extra": 1}') == {"type": "x", "extra": 1}


def test_truncate_output_utf8():
    data = ("я" * 3000).encode()
    out = protocol.truncate_output(data)
    assert len(out.encode()) <= protocol.MAX_OUTPUT and out == "я" * 2048


def test_ssl_context():
    assert ssl_context(panel_cfg("ws://x/agent/ws")) is None
    ctx = ssl_context(PanelConfig(url="wss://x/agent/ws", token="t"))
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    with pytest.raises(OSError):
        ssl_context(PanelConfig(url="wss://x/agent/ws", token="t", ca_file="/nonexistent/ca.pem"))


def test_agent_hello_and_samples(tmp_path):
    tok = tmp_path / "token"
    tok.write_text("good-token\n")

    async def main():
        panel = await FakePanel().start()
        cfg = parse_config({"node": "pi", "interval": 1, "panel": [
            {"url": panel.url, "token_file": str(tok), "insecure": True}]})
        agent = Agent(cfg, collectors=[ConstCollector({"interval": 1})], client_kwargs=FAST)
        stop = asyncio.Event()
        task = asyncio.create_task(agent.run(stop))
        await panel.wait(lambda: len(panel.of_type("sample")) >= 2, timeout=5)
        assert agent.clients[0].connected and agent.clients[0].interval_ms == 10000
        stop.set()
        await task
        await panel.stop()
        return panel

    panel = run(main())
    hello = panel.hellos[0]
    assert hello["type"] == "hello" and hello["v"] == 1 and hello["node"] == "pi"
    assert hello["token"] == "good-token"  # strip() убрал перевод строки
    assert hello["sensors"] == [{"id": "test.value", "kind": "state", "label": "Тест", "unit": ""}]
    assert hello["actions"] == [] and "info" in hello and isinstance(hello["ip"], list)
    s = panel.of_type("sample")[0]
    assert s["values"] == {"test.value": 42} and isinstance(s["ts"], int) and s["ts"] > 1_600_000_000_000


def test_bad_token_backoff_and_retry():
    async def main():
        panel = await FakePanel().start()
        client = PanelClient(panel_cfg(panel.url, token="wrong"), lambda t: {"type": "hello", "v": 1, "token": t},
                             **FAST)
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await panel.wait(lambda: len(panel.hellos) >= 3, timeout=5)
        stop.set()
        await task
        await panel.stop()
        return client

    client = run(main())
    assert client.last_error == "bad token" and not client.connected


def test_buffer_flush_after_reconnect():
    port = free_port()

    async def main():
        client = PanelClient(panel_cfg(f"ws://127.0.0.1:{port}/agent/ws", token="good-token", buffer=3),
                             lambda t: {"type": "hello", "v": 1, "token": t}, **FAST)
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        # Панели нет — выборки копятся, старые вытесняются (буфер 3)
        for ts in range(1, 6):
            client.publish(protocol.sample(ts, {"x": ts}))
        await asyncio.sleep(0.1)
        panel = await FakePanel().start(port)
        await panel.wait(lambda: len(panel.of_type("sample")) >= 3)
        # Обрыв: панель закрывает соединение, агент копит и досылает
        await panel.connections[0].close()
        await panel.wait(lambda: not client.connected)
        client.publish(protocol.sample(6, {"x": 6}))
        client.publish(protocol.sample(7, {"x": 7}))
        await panel.wait(lambda: len(panel.of_type("sample")) >= 5)
        stop.set()
        await task
        await panel.stop()
        return panel

    panel = run(main())
    assert [m["ts"] for m in panel.of_type("sample")] == [3, 4, 5, 6, 7]
    assert len(panel.hellos) == 2


def test_action_unknown_summary_and_garbage_ignored():
    async def on_hello(ws):
        await ws.send("garbage{")
        await ws.send(json.dumps({"type": "something_new", "x": 1}))
        await ws.send(json.dumps({"type": "summary", "online": 4, "total": 5, "alert": "Mini PC: диск / 91 %"}))
        await ws.send(json.dumps({"type": "action", "id": "a-17", "name": "rm_rf", "argv": ["rm", "-rf", "/"]}))

    async def main():
        panel = await FakePanel().start()
        panel.on_hello = on_hello
        cfg = parse_config({"node": "n", "panel": [{"url": panel.url, "token": "good-token", "insecure": True}]})
        agent = Agent(cfg, collectors=[], client_kwargs=FAST)
        stop = asyncio.Event()
        task = asyncio.create_task(agent.run(stop))
        await panel.wait(lambda: panel.of_type("action_result"))
        summary = agent.clients[0].summary
        stop.set()
        await task
        await panel.stop()
        return panel, summary

    panel, summary = run(main())
    (res,) = panel.of_type("action_result")
    assert res == {"type": "action_result", "id": "a-17", "name": "rm_rf", "ok": False, "code": None,
                   "output": "", "duration_ms": 0, "error": "unknown action"}
    assert summary == {"online": 4, "total": 5, "alert": "Mini PC: диск / 91 %"}


def test_panels_are_independent():
    dead = free_port()

    async def main():
        panel = await FakePanel().start()
        cfg = parse_config({"node": "n", "interval": 1, "panel": [
            {"url": f"ws://127.0.0.1:{dead}/agent/ws", "token": "x", "insecure": True},
            {"url": panel.url, "token": "good-token", "insecure": True},
        ]})
        agent = Agent(cfg, collectors=[ConstCollector({"interval": 1})], client_kwargs=FAST)
        stop = asyncio.Event()
        task = asyncio.create_task(agent.run(stop))
        await panel.wait(lambda: len(panel.of_type("sample")) >= 2)
        states = [c.connected for c in agent.clients]
        stop.set()
        await task
        await panel.stop()
        return states, agent

    states, agent = run(main())
    assert states == [False, True]
    assert len(agent.clients[0].buffer) >= 2  # мёртвая панель копит выборки
