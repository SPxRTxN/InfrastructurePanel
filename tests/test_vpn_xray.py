"""VPN на двух движках: sing-box, затем Xray; откат; белый список конфига Xray; юниты и пакетирование (без сети)."""
import asyncio
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from homelab_agent.vpn import SB_UNIT, XRAY_UNIT, VpnConfig, VpnError, VpnManager, prepare_xray
from test_vpn import CONFIG, PANELS, _fast, _script, fake_resolve

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"

XRAY = {
    "inbounds": [{"protocol": "socks", "listen": "127.0.0.1", "port": 10808, "settings": {"udp": True}}],
    "outbounds": [
        {"protocol": "vless", "tag": "proxy",
         "settings": {"vnext": [{"address": "vpn.example.org", "port": 443, "users": [{"id": "u"}]}]},
         "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {"publicKey": "k"}}},
        {"protocol": "freedom", "tag": "direct"},
    ],
}
TUN = {
    "inbounds": [{"type": "tun", "auto_route": True, "interface_name": "hl-vpn"}],
    "outbounds": [{"type": "socks", "tag": "vpn", "server": "127.0.0.1", "server_port": 10808},
                  {"type": "direct", "tag": "direct"}],
    "route": {"final": "vpn"},
}
MSG = {"id": "v-1", "mode": "all", "config": CONFIG, "xray": {"singbox": TUN, "xray": XRAY}}


def _manager(tmp_path, *, sb_ok=True, xray_ok=True, sb_check=0, xray_test=0):
    """sb_ok/xray_ok — «работает» ли движок (сторож); *_check — код проверки конфига."""
    cfg = VpnConfig(enabled=True, singbox=_script(tmp_path, "sing-box", f"sys.exit({sb_check})"),
                    xray=_script(tmp_path, "xray", f"sys.exit({xray_test})"), config_dir=str(tmp_path / "vpn"),
                    apply_argv=[_script(tmp_path, "apply", f"open(r'{tmp_path}/log','a').write('sb+')")],
                    stop_argv=[_script(tmp_path, "stop", f"open(r'{tmp_path}/log','a').write('sb-')")],
                    xray_apply_argv=[_script(tmp_path, "xapply", f"open(r'{tmp_path}/log','a').write('x+')")],
                    xray_stop_argv=[_script(tmp_path, "xstop", f"open(r'{tmp_path}/log','a').write('x-')")],
                    rollback_seconds=10)
    m = VpnManager(cfg, PANELS, resolve=fake_resolve, panel_ok=lambda: True, internet_ok=lambda: True, poll=0.05,
                   uid_of=lambda user: 987)
    m.ifaces = lambda: []
    m.tunnel_up = lambda iface: xray_ok if m.services == (XRAY_UNIT, SB_UNIT) else sb_ok
    return m


def _log(tmp_path):
    p = tmp_path / "log"
    return p.read_text() if p.exists() else ""


def test_singbox_first_and_xray_stays_off(tmp_path):
    m = _manager(tmp_path)
    res = asyncio.run(m.handle(MSG))
    assert res["ok"] and res["engine"] == "sing-box"
    assert not os.path.exists(m.xpath) and "x+" not in _log(tmp_path)


def test_singbox_refused_then_xray(tmp_path):
    m = _manager(tmp_path, sb_ok=False)
    res = asyncio.run(_fast(m, MSG))
    assert res["ok"] and res["engine"] == "xray" and "sing-box:" in res["detail"]
    assert os.path.exists(m.xpath) and os.path.exists(m.path)
    assert stat.S_IMODE(os.stat(m.xpath).st_mode) == 0o600
    assert _log(tmp_path).endswith("x+sb+")  # сначала Xray, затем TUN
    assert not os.path.exists(m.path + ".prev")


def test_both_refused_first_time_stops_everything(tmp_path):
    m = _manager(tmp_path, sb_ok=False, xray_ok=False)
    res = asyncio.run(_fast(m, MSG))
    assert not res["ok"] and res["engine"] == ""
    assert res["detail"].startswith("sing-box: ") and "; xray: " in res["detail"] and res["detail"].endswith("откат выполнен")
    assert not os.path.exists(m.path) and not os.path.exists(m.xpath)
    assert _log(tmp_path).endswith("sb-x-")


def test_both_refused_restores_previous_singbox(tmp_path):
    good = _manager(tmp_path)
    asyncio.run(good.handle({"id": "a", "mode": "all", "config": CONFIG}))
    before = open(good.path).read()
    bad = _manager(tmp_path, sb_ok=False, xray_ok=False)
    res = asyncio.run(_fast(bad, MSG))
    assert not res["ok"] and open(bad.path).read() == before and not os.path.exists(bad.xpath)
    assert not os.path.exists(bad.path + ".prev")
    assert _log(tmp_path).endswith("x-sb+")  # прежний движок запущен заново, Xray остановлен


def test_both_refused_restores_previous_xray(tmp_path):
    first = _manager(tmp_path, sb_ok=False)
    asyncio.run(_fast(first, MSG))
    x_before, s_before = open(first.xpath).read(), open(first.path).read()
    bad = _manager(tmp_path, sb_ok=False, xray_ok=False)
    res = asyncio.run(_fast(bad, MSG))
    assert not res["ok"]
    assert open(bad.xpath).read() == x_before and open(bad.path).read() == s_before
    assert _log(tmp_path).endswith("x+sb+")


def test_config_null_goes_straight_to_xray(tmp_path):
    m = _manager(tmp_path)
    res = asyncio.run(_fast(m, {**MSG, "config": None}))
    assert res["ok"] and res["engine"] == "xray"
    assert "sb+" not in _log(tmp_path).split("x+")[0]  # sing-box не пробовали до Xray


def test_xray_null_and_singbox_fail(tmp_path):
    m = _manager(tmp_path, sb_ok=False)
    res = asyncio.run(_fast(m, {**MSG, "xray": None}))
    assert not res["ok"] and "xray: сервер не поддерживается" in res["detail"]


def test_xray_test_failure_is_reported(tmp_path):
    m = _manager(tmp_path, sb_check=1, xray_test=1)
    res = asyncio.run(_fast(m, MSG))
    assert not res["ok"] and "Xray отверг конфиг" in res["detail"] and "sing-box отверг" in res["detail"]
    assert not os.path.exists(m.xpath[:-5] + ".new.json") and not os.path.exists(m.path + ".new")


def test_resolved_ip_and_exclude_uid_are_injected(tmp_path):
    m = _manager(tmp_path, sb_ok=False)
    asyncio.run(_fast(m, MSG))
    x = json.load(open(m.xpath))
    ob = x["outbounds"][0]
    assert ob["settings"]["vnext"][0]["address"] == "192.0.2.10"
    assert ob["streamSettings"]["realitySettings"]["serverName"] == "vpn.example.org"
    tun = json.load(open(m.path))["inbounds"][0]
    assert tun["exclude_uid"] == [987]
    assert {"192.0.2.10/32", "203.0.113.50/32"} <= set(tun["route_exclude_address"])
    assert XRAY["outbounds"][0]["settings"]["vnext"][0]["address"] == "vpn.example.org"  # исходник цел


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(api={"services": ["HandlerService"]}),
    lambda x: x.update(stats={}),
    lambda x: x.update(reverse={}),
    lambda x: x.update(policy={}),
    lambda x: x["inbounds"][0].update(listen="0.0.0.0"),
    lambda x: x["inbounds"][0].update(protocol="http"),
    lambda x: x["inbounds"].append(dict(x["inbounds"][0])),
    lambda x: x["outbounds"].append({"protocol": "dns"}),
    lambda x: x.update(outbounds=[{"protocol": "freedom"}]),
])
def test_xray_whitelist_rejects(mutate):
    bad = json.loads(json.dumps(XRAY))
    mutate(bad)
    with pytest.raises(VpnError):
        prepare_xray(bad, fake_resolve)


def test_xray_ok_returns_port_and_ips():
    conf, port, ips = prepare_xray(XRAY, fake_resolve)
    assert port == 10808 and ips == ["192.0.2.10"] and conf["outbounds"][1]["protocol"] == "freedom"


def test_xray_unresolvable_server():
    with pytest.raises(VpnError):
        prepare_xray(XRAY, lambda host: [])


def test_socks_outbound_must_point_to_local_xray(tmp_path):
    m = _manager(tmp_path, sb_ok=False)
    bad_tun = {**TUN, "outbounds": [{"type": "socks", "tag": "vpn", "server": "192.0.2.1", "server_port": 10808}]}
    res = asyncio.run(_fast(m, {**MSG, "xray": {"singbox": bad_tun, "xray": XRAY}}))
    assert not res["ok"] and "127.0.0.1" in res["detail"]


def test_socks_outbound_not_allowed_in_plain_singbox_config():
    from homelab_agent.vpn import prepare
    with pytest.raises(VpnError):
        prepare({**CONFIG, "outbounds": TUN["outbounds"]}, PANELS, fake_resolve)


def test_xray_unit_and_sudoers_and_setup():
    unit = (PKG / "homelab-vpn-xray.service").read_text(encoding="utf-8")
    assert "User=hl-xray" in unit
    assert "LoadCredential=xray.json:/opt/homelab-agent/etc/vpn/xray.json" in unit
    assert "ExecStart=/usr/local/bin/xray run -c %d/xray.json" in unit
    for want in ("NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true", "PrivateTmp=true",
                 "Restart=on-failure"):
        assert want in unit
    assert not re.search(r"^(Ambient|CapabilityBounding)\w*=\s*CAP_", unit, re.M)
    sb = (PKG / "homelab-vpn.service").read_text(encoding="utf-8")
    assert "LoadCredential=config.json" in sb
    inst = (PKG / "install.sh").read_text(encoding="utf-8")
    for line in ('$SYSTEMCTL restart homelab-vpn-xray.service', '$SYSTEMCTL stop homelab-vpn-xray.service',
                 'NOPASSWD: $SETUP_RUN $BIN/vpn-setup install-xray'):
        assert line in inst
    assert "useradd --system --no-create-home" in inst and "hl-xray" in inst
    assert "homelab-vpn-xray.service" in inst.split("for u in homelab-agent.service")[1].split("; do")[0]
    setup = (PKG / "vpn-setup").read_text(encoding="utf-8")
    assert "install-xray)" in setup and "Xray-linux-arm64-v8a.zip" in setup and "Xray-linux-arm32-v7a.zip" in setup
    # версия и три суммы закреплены: ровно 64 hex
    for arch in ("AMD64", "ARM64", "ARMV7"):
        m = re.search(rf'^XSUM_{arch}="([0-9a-f]*)"$', setup, re.M)
        assert m and len(m.group(1)) == 64
    assert re.search(r'^XRAY_VERSION="v\d+\.\d+\.\d+"$', setup, re.M)


def test_xray_checks_file_with_json_extension(tmp_path):
    """Настоящий Xray выбирает формат по расширению и не читает «xray.json.new» — проверяемый файл должен быть .json."""
    m = _manager(tmp_path, sb_ok=False, xray_test="0 if sys.argv[-1].endswith('.json') else 1")
    res = asyncio.run(_fast(m, MSG))
    assert res["ok"] and res["engine"] == "xray", res["detail"]
