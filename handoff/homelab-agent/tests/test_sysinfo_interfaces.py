"""hello.info.interfaces: psutil подменён, значения общие."""
import socket
from types import SimpleNamespace as NS

import psutil

from homelab_agent import sysinfo


def addr(family, address):
    return NS(family=family, address=address)


def patch(monkeypatch, addrs, stats):
    monkeypatch.setattr(sysinfo.psutil, "net_if_addrs", lambda: addrs)
    monkeypatch.setattr(sysinfo.psutil, "net_if_stats", lambda: stats)
    monkeypatch.setattr(sysinfo, "default_interface", lambda proc="/proc": "eth1")


def test_interfaces_filter_order_and_normalize(monkeypatch):
    addrs = {
        "lo": [addr(socket.AF_INET, "127.0.0.1")],
        "veth1": [addr(socket.AF_INET, "192.0.2.50")],
        "br-abc": [], "docker0": [],
        "eth0": [addr(psutil.AF_LINK, "02:00:00:00:00:00"), addr(socket.AF_INET, "192.0.2.10")],
        "eth1": [addr(psutil.AF_LINK, "02-00-00-00-00-AB"), addr(socket.AF_INET, "192.0.2.11"),
                 addr(socket.AF_INET6, "2001:db8::1%eth1")],
        "tun0": [addr(socket.AF_INET, "192.0.2.99")],
    }
    stats = {"eth0": NS(isup=False, speed=0), "eth1": NS(isup=True, speed=1000), "tun0": NS(isup=True, speed=-1)}
    patch(monkeypatch, addrs, stats)
    res = sysinfo.interfaces()
    assert [i["name"] for i in res] == ["eth1", "eth0", "tun0"]
    assert res[0] == {"name": "eth1", "mac": "02:00:00:00:00:ab", "ipv4": ["192.0.2.11"],
                      "ipv6": ["2001:db8::1"], "up": True, "speed": 1000}
    assert res[1]["up"] is False and res[2]["speed"] == 0 and res[2]["mac"] == ""


def test_interfaces_limits(monkeypatch):
    many = {f"eth{i}": [addr(socket.AF_INET, f"192.0.2.{i}")] for i in range(30)}
    patch(monkeypatch, many, {})
    assert len(sysinfo.interfaces()) == 16
    big = {"e" * 40: [addr(socket.AF_INET, "192.0.2.%d" % i) for i in range(12)]}
    patch(monkeypatch, big, {})
    r = sysinfo.interfaces()[0]
    assert len(r["name"]) == 32 and len(r["ipv4"]) == 8 and r["up"] is False


def test_interfaces_psutil_error(monkeypatch):
    def boom():
        raise OSError("x")
    monkeypatch.setattr(sysinfo.psutil, "net_if_addrs", boom)
    assert sysinfo.interfaces() == []
    assert "interfaces" in sysinfo.collect()["info"]
