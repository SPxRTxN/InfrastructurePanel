"""VPN узла: проверка присланного конфига, обход для панели, применение, сторож и откат (без root и сети)."""
import asyncio
import json
import os
import stat
import sys

import pytest

from homelab_agent.config import parse_config
from homelab_agent.vpn import VpnConfig, VpnError, VpnManager, parse_vpn, prepare

CONFIG = {
    "inbounds": [{"type": "tun", "auto_route": True, "route_exclude_address": ["192.168.0.0/16"]}],
    "outbounds": [{"type": "vless", "tag": "vpn", "server": "vpn.example.org", "server_port": 443, "uuid": "u"},
                  {"type": "direct", "tag": "direct"}],
    "route": {"final": "vpn"},
}
PANELS = ["ws://192.168.1.39:8000/agent/ws", "wss://dash.example/agent/ws"]


def fake_resolve(host):
    return {"192.168.1.39": ["192.168.1.39"], "dash.example": ["203.0.113.50", "2001:db8::1"]}.get(host, [])


def test_prepare_adds_panels_to_bypass():
    out = prepare(CONFIG, PANELS, fake_resolve)
    ex = out["inbounds"][0]["route_exclude_address"]
    assert {"192.168.0.0/16", "192.168.1.39/32", "203.0.113.50/32", "2001:db8::1/128"} <= set(ex)
    assert CONFIG["inbounds"][0]["route_exclude_address"] == ["192.168.0.0/16"]  # исходник не меняется


@pytest.mark.parametrize("bad", [
    {**CONFIG, "experimental": {"clash_api": {"external_controller": "0.0.0.0:9090"}}},
    {**CONFIG, "inbounds": [{"type": "mixed", "listen": "0.0.0.0", "listen_port": 1080}]},
    {**CONFIG, "inbounds": CONFIG["inbounds"] * 2},
    {**CONFIG, "outbounds": [{"type": "tor"}]},
    "not a dict",
])
def test_prepare_rejects(bad):
    with pytest.raises(VpnError):
        prepare(bad, PANELS, fake_resolve)


def test_parse_vpn_and_config_section():
    assert parse_vpn(None).enabled is False
    assert parse_vpn({"enabled": True, "rollback_seconds": 30}).rollback_seconds == 30
    for bad in ({"enabled": "yes"}, {"rollback_seconds": 1}, {"apply_argv": "systemctl restart"}, {"bogus": 1}):
        with pytest.raises(ValueError):
            parse_vpn(bad)
    cfg = parse_config({"node": "pi", "vpn": {"enabled": True}})
    assert cfg.vpn == {"enabled": True}


def _script(tmp_path, name, code):
    p = tmp_path / name
    p.write_text(f"#!{sys.executable}\nimport sys\n{code}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


def _manager(tmp_path, *, check_code=0, apply_code=0, panel=True, net=True):
    cfg = VpnConfig(enabled=True, singbox=_script(tmp_path, "sing-box", f"sys.exit({check_code})"),
                    config_dir=str(tmp_path / "vpn"),
                    apply_argv=[_script(tmp_path, "apply", f"open(r'{tmp_path}/applied','a').write('x'); sys.exit({apply_code})")],
                    stop_argv=[_script(tmp_path, "stop", f"open(r'{tmp_path}/stopped','w').write('x')")],
                    rollback_seconds=10)
    return VpnManager(cfg, PANELS, resolve=fake_resolve, panel_ok=lambda: panel, internet_ok=lambda: net, poll=0.05)


def test_apply_ok_writes_private_config(tmp_path):
    m = _manager(tmp_path)
    res = asyncio.run(m.handle({"type": "vpn", "id": "v-1", "mode": "all", "config": CONFIG}))
    assert res == {"type": "vpn_result", "id": "v-1", "ok": True, "detail": "туннель поднят: панель и интернет доступны"}
    assert stat.S_IMODE(os.stat(m.path).st_mode) == 0o600
    assert "192.168.1.39/32" in json.load(open(m.path))["inbounds"][0]["route_exclude_address"]


def test_check_rejects_without_touching_current(tmp_path):
    m = _manager(tmp_path)
    asyncio.run(m.handle({"id": "v-1", "mode": "all", "config": CONFIG}))
    before = open(m.path).read()
    bad = _manager(tmp_path, check_code=1)
    res = asyncio.run(bad.handle({"id": "v-2", "mode": "all", "config": CONFIG}))
    assert res["ok"] is False and "sing-box отверг" in res["detail"]
    assert open(m.path).read() == before


def test_rollback_to_previous_when_no_connectivity(tmp_path):
    good = _manager(tmp_path)
    asyncio.run(good.handle({"id": "v-1", "mode": "all", "config": CONFIG}))
    before = open(good.path).read()
    broken = _manager(tmp_path, net=False)
    other = {**CONFIG, "outbounds": [{**CONFIG["outbounds"][0], "server": "dead.example"}, CONFIG["outbounds"][1]]}
    broken.cfg.rollback_seconds = 10
    res = asyncio.run(_fast(broken, {"id": "v-2", "mode": "all", "config": other}))
    assert res["ok"] is False and "откат выполнен" in res["detail"]
    assert open(good.path).read() == before  # вернулся прежний конфиг
    assert not os.path.exists(good.path + ".prev")


def test_rollback_first_time_stops_service(tmp_path):
    m = _manager(tmp_path, panel=False)
    res = asyncio.run(_fast(m, {"id": "v-1", "mode": "all", "config": CONFIG}))
    assert res["ok"] is False and not os.path.exists(m.path) and os.path.exists(tmp_path / "stopped")


def test_off(tmp_path):
    m = _manager(tmp_path)
    asyncio.run(m.handle({"id": "v-1", "mode": "all", "config": CONFIG}))
    res = asyncio.run(m.handle({"id": "v-1", "mode": "off"}))
    assert res["ok"] and not os.path.exists(m.path)


async def _fast(m, msg):
    # Сторож с минимальным временем, чтобы тест не ждал 10 с
    m.cfg.rollback_seconds = 10
    import homelab_agent.vpn as mod
    real = mod.time.monotonic
    start = real()
    mod.time.monotonic = lambda: start + (real() - start) * 100
    try:
        return await m.handle(msg)
    finally:
        mod.time.monotonic = real


def test_hello_features(tmp_path):
    from homelab_agent.runner import Agent

    cfg = parse_config({"node": "pi", "vpn": {"enabled": True, "config_dir": str(tmp_path)},
                        "panel": [{"url": "ws://127.0.0.1:1/agent/ws", "token": "t", "insecure": True}]})
    agent = Agent(cfg, collectors=[])
    assert agent.hello("t")["features"] == ["vpn"]
    assert Agent(parse_config({"node": "pi"}), collectors=[]).hello("t")["features"] == []
