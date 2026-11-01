"""Wake-on-LAN агента: magic packet, подменённый сокет, проверка входных данных, объявление возможности."""
import asyncio
import types

import pytest

import homelab_agent.wol as wol
from homelab_agent.config import parse_config
from homelab_agent.runner import Agent
from homelab_agent.wol import WolSender, magic_packet, parse_wol


class FakeSocket:
    sent: list = []
    opts: list = []

    def __init__(self, *a, **kw):
        pass

    def setsockopt(self, *a):
        FakeSocket.opts.append(a)

    def sendto(self, data, addr):
        FakeSocket.sent.append((data, addr))

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_socket(monkeypatch):
    FakeSocket.sent, FakeSocket.opts = [], []
    # Подменяем весь модуль socket только внутри wol — asyncio рядом использует настоящий
    monkeypatch.setattr(wol, "socket", types.SimpleNamespace(
        socket=FakeSocket, AF_INET=2, SOCK_DGRAM=2, SOL_SOCKET=1, SO_BROADCAST=6))


def test_magic_packet():
    p = magic_packet("AA:bb:cc:dd:ee:ff")
    assert len(p) == 102 and p[:6] == b"\xff" * 6
    assert p[6:] == bytes.fromhex("aabbccddeeff") * 16


@pytest.mark.parametrize("bad", ["", "aa:bb", "gg:bb:cc:dd:ee:ff", "aabbccddeeff", "aa:bb:cc:dd:ee:ff:00", None])
def test_bad_mac_refused(bad):
    with pytest.raises(ValueError):
        magic_packet(bad)


def test_handle_sends_udp_9():
    res = asyncio.run(WolSender(parse_wol({"enabled": True})).handle(
        {"type": "wol", "id": "w-1", "mac": "aa:bb:cc:dd:ee:ff", "broadcast": "255.255.255.255"}))
    assert res["type"] == "wol_result" and res["id"] == "w-1" and res["ok"] is True
    data, addr = FakeSocket.sent[0]
    assert addr == ("255.255.255.255", 9) and len(data) == 102


def test_handle_refuses_bad_input():
    s = WolSender(parse_wol({"enabled": True}))
    bad_mac = asyncio.run(s.handle({"id": "w-2", "mac": "nope", "broadcast": "255.255.255.255"}))
    assert bad_mac["ok"] is False and "MAC" in bad_mac["detail"]
    # Не широковещательный адрес — агент не становится источником UDP на произвольный хост
    unicast = asyncio.run(s.handle({"id": "w-3", "mac": "aa:bb:cc:dd:ee:ff", "broadcast": "192.168.1.39"}))
    assert unicast["ok"] is False
    assert FakeSocket.sent == []


def test_config_and_features():
    assert parse_wol(None).enabled is False
    with pytest.raises(ValueError):
        parse_wol({"enabled": True, "port": 80})
    with pytest.raises(ValueError):
        parse_wol({"bogus": 1})
    off = Agent(parse_config({"node": "pi"}), collectors=[])
    assert off.hello("t")["features"] == [] and off.wol is None
    on = Agent(parse_config({"node": "pi", "wol": {"enabled": True}}), collectors=[])
    assert on.hello("t")["features"] == ["wol"]
