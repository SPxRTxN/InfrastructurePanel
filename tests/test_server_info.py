"""Страница сервера — вся информация (сеть, интерфейсы, VPN, датчики); страница узла — только настройки."""
import json

import pytest

from app.models import Node, Server, VpnAssignment, VpnSubscription
from app.services import agents

IFACES = [{"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ipv4": ["192.0.2.5"], "ipv6": [], "up": True, "speed": 1000}]


@pytest.fixture
def pair(db):
    srv = Server(name="Mini", host="192.0.2.39", collector="agent")
    n = Node(name="mini", server=srv, version="0.5.0")
    agents.issue_token(n)
    n.info_json = json.dumps({"os": "Linux", "features": ["tunnel", "vpn"], "interfaces": IFACES})
    db.add_all([srv, n])
    db.commit()
    return srv, n


def test_parse_interfaces_strict():
    raw = IFACES + [
        {"name": "bad name!", "mac": "aa:bb:cc:dd:ee:02"},
        {"name": "wlan0", "mac": "AA:BB", "ipv4": ["192.0.2.6", "nope", "::1"], "ipv6": ["2001:db8::1", "1.2.3.4"],
         "up": "yes", "speed": -5, "extra": "x"},
        "junk",
    ]
    out = agents.parse_interfaces(raw)
    assert [i["name"] for i in out] == ["eth0", "wlan0"]
    assert out[1] == {"name": "wlan0", "mac": "", "ipv4": ["192.0.2.6"], "ipv6": ["2001:db8::1"], "up": False, "speed": 0}
    assert len(agents.parse_interfaces([{"name": f"i{k}"} for k in range(40)])) == 16
    assert agents.parse_interfaces("x") == []


def test_server_page_shows_network_and_vpn(admin, viewer, db, pair):
    srv, n = pair
    sub = VpnSubscription(name="s", url_enc="x")
    db.add(sub)
    db.commit()
    db.add(VpnAssignment(node_id=n.id, subscription_id=sub.id, server_key="k", server_name="Vpn-Example",
                         engine="xray", mode="all", status="applied"))
    db.commit()
    page = admin.get(f"/servers/{srv.id}").text
    assert 'id="net"' in page and "aa:bb:cc:dd:ee:01" in page and "1000 Мбит/с" in page and "192.0.2.5" in page
    assert "Vpn-Example" in page and "xray" in page and "Туннель консоли" in page
    assert "chartzoom.js" in page and 'id="node-charts"' in page
    seen = viewer.get(f"/servers/{srv.id}").text
    assert "включён" in seen and "Vpn-Example" not in seen  # имя VPN-сервера — только с settings.manage


def test_node_page_is_settings_only(admin, db, pair):
    srv, n = pair
    page = admin.get(f"/nodes/{n.id}").text
    assert f'href="/servers/{srv.id}"' in page and "Информация — на странице сервера" in page
    for gone in ("Датчики (", "История датчиков", "<h3>Диски", "aa:bb:cc:dd:ee:01"):
        assert gone not in page, gone
    assert 'id="settings"' in page and 'id="actions"' in page and "tile-grid" in page


def test_allow_vpn_mentioned_once(admin, db, pair):
    srv, n = pair
    parts = [admin.get(f"/nodes/{n.id}").text, admin.get(f"/servers/{srv.id}/agent-install").text,
             admin.get(f"/servers/{srv.id}/vpn").text]
    assert sum(p.count("Разрешить VPN") for p in parts) == 1
    assert "Разрешить VPN" in parts[1]


def test_only_charts_full_width_and_tiles_movable(admin, db, pair):
    """На всю ширину — только графики; у каждой плитки в сетке есть id (порядок перетаскивания запоминается по нему)."""
    import re
    server, node = pair
    for url in (f"/servers/{server.id}", f"/nodes/{node.id}"):
        page = admin.get(url).text
        assert page.count("span-2") == (1 if url.startswith("/servers") else 0), url
        assert "Sortable.min.js" not in page  # перетаскивания нет: порядок задан в шаблоне
        grid = page[page.index('class="tile-grid"'):]
        assert not re.search(r'<section class="card[^"]*"(?![^>]*\bid=)', grid), url
