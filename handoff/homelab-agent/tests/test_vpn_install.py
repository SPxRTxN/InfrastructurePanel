"""VPN: формат DNS sing-box 1.14, автоустановка sing-box через vpn-setup, info.vpn (без root, сети и sudo)."""
import asyncio
import stat
import sys

import pytest

from homelab_agent import vpn as vpn_mod
from homelab_agent.vpn import VpnConfig, VpnError, VpnManager, info, prepare

CONFIG = {
    "inbounds": [{"type": "tun", "auto_route": True}],
    "outbounds": [{"type": "vless", "tag": "vpn", "server": "vpn.example.org", "server_port": 443, "uuid": "u"},
                  {"type": "direct", "tag": "direct"}],
    "route": {"final": "vpn"},
}
PANELS = ["wss://dash.example/agent/ws"]


def fake_resolve(host):
    return ["192.0.2.50"]


NEW_DNS = {
    **CONFIG,
    "dns": {"servers": [{"type": "tls", "tag": "remote", "server": "192.0.2.53", "detour": "vpn"},
                        {"type": "local", "tag": "local"}],
            "rules": [{"domain": ["vpn.example.org"], "server": "local"}], "final": "remote"},
    "route": {"final": "vpn", "default_domain_resolver": "local"},
}


def test_prepare_accepts_new_dns_format():
    out = prepare(NEW_DNS, PANELS, fake_resolve)
    assert out["dns"]["servers"][0]["type"] == "tls"
    assert out["route"]["default_domain_resolver"] == "local"


@pytest.mark.parametrize("bad", [
    {**CONFIG, "dns": {"servers": [{"type": "evil", "tag": "x"}]}},
    {**CONFIG, "dns": {"servers": "192.0.2.1"}},
    {**CONFIG, "route": {"default_domain_resolver": 5}},
])
def test_prepare_rejects_bad_dns_route(bad):
    with pytest.raises(VpnError):
        prepare(bad, PANELS, fake_resolve)


def _manager(tmp_path, singbox, setup="/opt/hl/vpn-setup"):
    cfg = VpnConfig(enabled=True, singbox=str(singbox), setup=setup, config_dir=str(tmp_path / "vpn"),
                    rollback_seconds=10)
    m = VpnManager(cfg, PANELS, resolve=fake_resolve, panel_ok=lambda: True, internet_ok=lambda: True, tunnel_up=lambda iface: True, poll=0.05)
    m.ifaces = lambda: [{"name": "hl-vpn"}]
    return m


def test_auto_install_singbox_via_setup(tmp_path, monkeypatch):
    singbox = tmp_path / "sing-box"
    calls = []

    async def fake_run(argv, timeout=30):
        calls.append((argv, timeout))
        if argv[:3] == ["sudo", "-n", "systemd-run"]:
            singbox.write_text("x")  # помощник «поставил» sing-box
        return 0, ""

    monkeypatch.setattr(vpn_mod, "_run", fake_run)
    res = asyncio.run(_manager(tmp_path, singbox).handle({"type": "vpn", "id": "v-9", "mode": "all", "config": CONFIG}))
    assert res["ok"] is True
    assert calls[0] == (["sudo", "-n", "systemd-run", "--wait", "--pipe", "--collect", "--quiet", "/opt/hl/vpn-setup", "install"], 300)


def test_auto_install_failure_reported(tmp_path, monkeypatch):
    async def fake_run(argv, timeout=30):
        return 1, "нет сети"

    monkeypatch.setattr(vpn_mod, "_run", fake_run)
    res = asyncio.run(_manager(tmp_path, tmp_path / "none").handle(
        {"type": "vpn", "id": "v-8", "mode": "all", "config": CONFIG}))
    assert res["ok"] is False and "не удалось установить sing-box" in res["detail"]


def test_info_vpn(tmp_path):
    setup = tmp_path / "vpn-setup"
    nox = str(tmp_path / "no-xray")
    assert info(VpnConfig(singbox=str(tmp_path / "none"), xray=nox, setup=str(setup))) == {
        "singbox": "", "xray": "", "setup": False}
    setup.write_text("x")
    box = tmp_path / "sing-box"
    box.write_text(f"#!{sys.executable}\nprint('sing-box version 1.14.4')\n")
    box.chmod(box.stat().st_mode | stat.S_IXUSR)
    xr = tmp_path / "xray"
    xr.write_text(f"#!{sys.executable}\nprint('Xray 1.8.24 (Xray, Penetrates Everything.) custom')\n")
    xr.chmod(xr.stat().st_mode | stat.S_IXUSR)
    assert info(VpnConfig(singbox=str(box), xray=str(xr), setup=str(setup))) == {
        "singbox": "1.14.4", "xray": "1.8.24", "setup": True}
