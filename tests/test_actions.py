import asyncio
import sys

import pytest

from fakepanel import FakePanel
from homelab_agent.actions import ActionRunner, AutoRecovery, parse_actions
from homelab_agent.config import parse_config
from homelab_agent.runner import Agent

PY = sys.executable
FAST = {"backoff_min": 0.05, "backoff_max": 0.2, "bad_token_max": 0.3, "welcome_timeout": 2}


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def runner(section, clock=None):
    specs = parse_actions(section)
    return ActionRunner(specs, clock=clock) if clock else ActionRunner(specs)


@pytest.mark.parametrize("section", [
    {"Bad-Name": {"argv": ["true"]}},
    {"a": {"argv": []}},
    {"a": {"argv": "systemctl restart nginx"}},
    {"a": {"argv": ["true"], "limit": "3 per hour"}},
    {"a": {"argv": ["true"], "limit": "0/h"}},
    {"a": {"argv": ["true"], "timeout": 0}},
    {"a": {"argv": ["true"], "shell": True}},
    {"a": {"argv": ["true"], "auto": {"when": "x == y"}}},
    {"a": {"argv": ["true"], "auto": {"when": "__import__('os') == 1"}}},
])
def test_parse_validation(section):
    with pytest.raises(ValueError):
        parse_actions(section)


def test_parse_ok():
    s = parse_actions({"restart_nginx": {"argv": ["sudo", "-n", "systemctl", "restart", "nginx.service"],
                                         "limit": "3/h", "label": "Перезапустить nginx",
                                         "auto": {"when": "systemd.nginx.service.active == 0", "for": 60}}})
    spec = s["restart_nginx"]
    assert spec.limit_count == 3 and spec.limit_window == 3600 and spec.timeout == 30
    assert spec.auto.sensor == "systemd.nginx.service.active" and spec.auto.hold == 60


def test_unknown_action():
    res = run(runner({}).run("a-1", "rm_rf"))
    assert res["ok"] is False and res["error"] == "unknown action" and res["id"] == "a-1"


def test_success_and_failure_codes():
    r = runner({"hello": {"argv": ["echo", "привет"]}, "fail": {"argv": [PY, "-c", "import sys; sys.exit(3)"]}})
    ok = run(r.run("1", "hello"))
    assert ok["ok"] is True and ok["code"] == 0 and ok["output"] == "привет\n" and ok["duration_ms"] >= 0
    bad = run(r.run("2", "fail"))
    assert bad["ok"] is False and bad["code"] == 3 and "error" not in bad
    assert r.last["name"] == "fail" and r.last["ok"] is False


def test_no_shell_expansion():
    res = run(runner({"echo": {"argv": ["echo", "$(id)", ";", "ls"]}}).run("1", "echo"))
    assert res["output"] == "$(id) ; ls\n"


def test_output_truncated():
    res = run(runner({"big": {"argv": [PY, "-c", "print('x' * 100000)"]}}).run("1", "big"))
    assert res["ok"] and len(res["output"].encode()) == 4096


def test_timeout_kills():
    res = run(runner({"slow": {"argv": [PY, "-c", "import time; time.sleep(30)"], "timeout": 0.3}}).run("1", "slow"))
    assert res["ok"] is False and res["error"] == "timeout" and res["duration_ms"] < 5000


def test_busy():
    r = runner({"slow": {"argv": [PY, "-c", "import time; time.sleep(0.5)"]}, "fast": {"argv": ["true"]}})

    async def main():
        first = asyncio.create_task(r.run("1", "slow"))
        await asyncio.sleep(0.1)
        second = await r.run("2", "fast")
        return await first, second

    first, second = run(main())
    assert first["ok"] is True and second["error"] == "busy"


def test_rate_limit_with_window():
    now = [1000.0]
    r = runner({"t": {"argv": ["true"], "limit": "2/h"}}, clock=lambda: now[0])
    assert run(r.run("1", "t"))["ok"]
    assert run(r.run("2", "t"))["ok"]
    assert run(r.run("3", "t"))["error"] == "rate limited"
    now[0] += 3600
    assert run(r.run("4", "t"))["ok"]


def test_env_is_clean(monkeypatch):
    monkeypatch.setenv("HOMELAB_SECRET", "leak")
    res = run(runner({"env": {"argv": ["env"]}}).run("1", "env"))
    assert "HOMELAB_SECRET" not in res["output"] and "PATH=" in res["output"]


def test_auto_recovery_hold():
    now = [0.0]
    specs = parse_actions({"r": {"argv": ["true"], "auto": {"when": "systemd.nginx.service.active == 0", "for": 60}}})
    auto = AutoRecovery(specs, clock=lambda: now[0])
    assert auto.due({"systemd.nginx.service.active": 0}) == []
    now[0] = 30
    assert auto.due({"systemd.nginx.service.active": 0}) == []
    now[0] = 61
    assert auto.due({"systemd.nginx.service.active": 0}) == ["r"]
    now[0] = 62
    assert auto.due({"systemd.nginx.service.active": 0}) == []  # отсчёт заново
    assert auto.due({"systemd.nginx.service.active": 1}) == []
    now[0] = 200
    assert auto.due({}) == []  # нет датчика — не срабатывает


def test_panel_action_end_to_end():
    async def on_hello(ws):
        import json

        await ws.send(json.dumps({"type": "action", "id": "a-17", "name": "hello"}))

    async def main():
        panel = await FakePanel().start()
        panel.on_hello = on_hello
        cfg = parse_config({"node": "n", "panel": [{"url": panel.url, "token": "good-token", "insecure": True}],
                            "actions": {"hello": {"argv": ["echo", "ok"], "label": "Проверка"}}})
        agent = Agent(cfg, collectors=[], client_kwargs=FAST)
        stop = asyncio.Event()
        task = asyncio.create_task(agent.run(stop))
        await panel.wait(lambda: panel.of_type("action_result"))
        stop.set()
        await task
        await panel.stop()
        return panel

    panel = run(main())
    assert panel.hellos[0]["actions"] == [{"name": "hello", "label": "Проверка"}]
    (res,) = panel.of_type("action_result")
    assert res["id"] == "a-17" and res["ok"] is True and res["code"] == 0 and res["output"] == "ok\n"


def test_auto_recovery_in_agent_sends_event():
    async def main():
        cfg = parse_config({"node": "n", "panel": [{"url": "wss://x/agent/ws", "token": "t"}],
                            "actions": {"fix": {"argv": ["true"], "label": "Починить",
                                                "auto": {"when": "test.up == 0", "for": 0}}}})
        agent = Agent(cfg, collectors=[])
        agent.publish({"test.up": 0})
        await agent.after_sample({"test.up": 0})
        await asyncio.gather(*agent._bg)
        return agent

    agent = run(main())
    (ev,) = agent.clients[0].control
    assert ev["type"] == "event" and ev["level"] == "warning" and "Починить" in ev["text"]
    assert agent.actions.last["source"] == "auto"
