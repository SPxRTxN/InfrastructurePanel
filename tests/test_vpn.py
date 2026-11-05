"""VPN: разбор подписок, секреты не утекают, конфиг sing-box, права, отправка узлу и результат."""
import base64
import json
import time

import pytest
from sqlalchemy import select

from app.models import Node, SettingValue, VpnAssignment, VpnSubscription
from app.routers import vpn as vpn_router
from app.services import vpn
from app.services.security import hash_token

# Выдуманные ключи — только для тестов
UUID = "11111111-2222-3333-4444-555555555555"
VLESS = (f"vless://{UUID}@vpn1.example.org:443?type=tcp&security=reality&sni=www.microsoft.com&fp=chrome"
         f"&pbk=PUBKEYexample123&sid=abcd&flow=xtls-rprx-vision#%D0%9D%D0%B8%D0%B4%D0%B5%D1%80%D0%BB%D0%B0%D0%BD%D0%B4%D1%8B")
VMESS = "vmess://" + base64.b64encode(json.dumps({"v": "2", "ps": "Германия", "add": "203.0.113.5", "port": "8443",
                                                   "id": UUID, "aid": "0", "net": "ws", "path": "/ws",
                                                   "host": "cdn.example.org", "tls": "tls"}).encode()).decode()
TROJAN = "trojan://trojan-secret-pass@198.51.100.7:443?type=grpc&serviceName=gun&sni=t.example.org#Trojan"
SS_NEW = "ss://" + base64.urlsafe_b64encode(b"chacha20-ietf-poly1305:ss-secret-pass").decode().rstrip("=") + "@192.0.2.9:8388#SS"
HY2 = "hysteria2://hy2-secret@192.0.2.10:443?sni=h.example.org&obfs=salamander&obfs-password=ob-secret#HY2"
BODY = base64.b64encode("\n".join([VLESS, VMESS, TROJAN, SS_NEW, HY2, "socks://nope@1.2.3.4:1080#x"]).encode()).decode()
SECRETS = [UUID, "trojan-secret-pass", "ss-secret-pass", "hy2-secret", "ob-secret", "PUBKEYexample123"]


def test_parse_all_formats():
    servers, errors = vpn.parse(BODY)
    by = {s["protocol"]: s for s in servers}
    assert set(by) == {"vless", "vmess", "trojan", "ss", "hysteria2"}
    assert len(errors) == 1 and "socks" in errors[0]
    assert by["vless"]["name"] == "Нидерланды" and by["vless"]["security"] == "reality"
    assert by["vless"]["public_key"] == "PUBKEYexample123" and by["vless"]["flow"] == "xtls-rprx-vision"
    assert by["vmess"]["host"] == "203.0.113.5" and by["vmess"]["transport"] == "ws" and by["vmess"]["port"] == 8443
    assert by["trojan"]["service_name"] == "gun"
    assert by["ss"]["method"] == "chacha20-ietf-poly1305" and by["ss"]["password"] == "ss-secret-pass"
    assert by["hysteria2"]["obfs"] == "salamander"
    # Простой список ссылок (не base64) тоже понимается
    assert len(vpn.parse(VLESS + "\n" + TROJAN)[0]) == 2
    with pytest.raises(vpn.SubscriptionError):
        vpn.links("<html>not a subscription</html>")


def test_public_hides_secrets():
    servers, _ = vpn.parse(BODY)
    text = json.dumps([vpn.public(s) for s in servers], ensure_ascii=False)
    assert not any(s in text for s in SECRETS)


def test_singbox_config():
    servers, _ = vpn.parse(BODY)
    by = {s["protocol"]: s for s in servers}
    cfg = vpn.singbox_config(by["vless"], bypass=["192.0.2.39"])
    out = cfg["outbounds"][0]
    assert out["type"] == "vless" and out["tls"]["reality"]["public_key"] == "PUBKEYexample123"
    tun = cfg["inbounds"][0]
    assert tun["auto_route"] and "192.168.0.0/16" in tun["route_exclude_address"]
    assert "192.0.2.39/32" in tun["route_exclude_address"]
    assert cfg["route"]["final"] == "vpn"
    assert vpn.outbound(by["vmess"])["transport"] == {"type": "ws", "path": "/ws", "headers": {"Host": "cdn.example.org"}}
    assert vpn.outbound(by["trojan"])["transport"]["service_name"] == "gun"
    assert vpn.outbound(by["ss"])["type"] == "shadowsocks"
    assert vpn.outbound(by["hysteria2"])["obfs"]["type"] == "salamander"
    # IP-адрес VPN-сервера — мимо туннеля (иначе петля)
    assert "203.0.113.5/32" in vpn.singbox_config(by["vmess"], [])["inbounds"][0]["route_exclude_address"]
    with pytest.raises(vpn.SubscriptionError):
        vpn.outbound({**by["vless"], "transport": "kcp"})


@pytest.fixture
def subscription(admin, db, monkeypatch):
    monkeypatch.setattr(vpn, "fetch", lambda url, insecure: BODY)
    r = admin.post("/vpn/subscriptions", data={"name": "Мой VPN", "url": "https://203.0.113.1:2096/sub/token-abc",
                                               "insecure": "on"}, follow_redirects=False)
    assert r.status_code == 303
    return db.scalar(select(VpnSubscription))


def test_subscription_stored_encrypted_and_page(admin, db, subscription):
    assert subscription.count == 5 and subscription.insecure and "пропущено 1" in subscription.error
    assert "token-abc" not in subscription.url_enc and UUID not in subscription.servers_enc
    page = admin.get("/vpn").text
    assert "Нидерланды" in page and "vpn1.example.org:443" in page
    assert not any(s in page for s in SECRETS) and "token-abc" not in page


def test_permissions(user, viewer, subscription):
    for c in (user, viewer):
        assert c.get("/vpn").status_code == 403
        assert c.post("/vpn/subscriptions", data={"url": "https://x"}).status_code == 403
    assert 'href="/vpn"' not in viewer.get("/nodes").text  # вкладку не видно без settings.manage


def test_fetch_error_saved(admin, db, monkeypatch):
    def boom(url, insecure):
        raise vpn.SubscriptionError("TLS: CERTIFICATE_VERIFY_FAILED — …")
    monkeypatch.setattr(vpn, "fetch", boom)
    admin.post("/vpn/subscriptions", data={"url": "https://203.0.113.1/sub"})
    assert "CERTIFICATE_VERIFY_FAILED" in db.scalar(select(VpnSubscription)).error


def _node(db, features):
    token = "tok-" + "x" * 30
    n = Node(name="pi", token_hash=hash_token(token), info_json=json.dumps({"features": features}))
    db.add(n)
    db.commit()
    return n, token


def test_assign_unsupported_node(admin, db, subscription):
    n, _ = _node(db, [])
    key = vpn.unpack(subscription.servers_enc)[0]["key"]
    admin.post("/vpn/assign", data={"node_id": n.id, "server": f"{subscription.id}:{key}"})
    a = db.scalar(select(VpnAssignment))
    assert a.status == "unsupported" and a.mode == "all"
    assert admin.post("/vpn/assign", data={"node_id": n.id, "server": f"{subscription.id}:nope"}).status_code == 400


def test_assign_sent_to_agent_and_result(admin, db, subscription):
    n, token = _node(db, ["vpn"])
    key = vpn.unpack(subscription.servers_enc)[0]["key"]
    hello = {"type": "hello", "v": 1, "node": "pi", "token": token, "sensors": [], "features": ["vpn"]}
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            ws.send_text(json.dumps(hello))
            assert json.loads(ws.receive_text())["type"] == "welcome"
            admin.post("/vpn/assign", data={"node_id": n.id, "server": f"{subscription.id}:{key}"})
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "vpn" and msg["mode"] == "all" and msg["config"]["outbounds"][0]["uuid"] == UUID
            assert "uuid" not in msg["server"]
            ws.send_text(json.dumps({"type": "vpn_result", "id": msg["id"], "ok": True, "detail": "туннель поднят"}))
            for _ in range(50):
                db.expire_all()
                if db.scalar(select(VpnAssignment)).status == "applied":
                    break
                time.sleep(0.05)
            assert db.scalar(select(VpnAssignment)).detail == "туннель поднят"
            # Выключение
            admin.post(f"/vpn/assign/{n.id}/off")
            assert json.loads(ws.receive_text()) == {"type": "vpn", "id": msg["id"], "mode": "off"}
    # Подписку, которую использует узел, удалить нельзя, пока VPN не выключен; выключен — можно
    assert admin.post(f"/vpn/subscriptions/{subscription.id}/delete", follow_redirects=False).status_code == 303


def test_pending_sent_on_hello(admin, db, subscription):
    n, token = _node(db, ["vpn"])
    key = vpn.unpack(subscription.servers_enc)[0]["key"]
    admin.post("/vpn/assign", data={"node_id": n.id, "server": f"{subscription.id}:{key}"})
    assert db.scalar(select(VpnAssignment)).status == "pending"
    with admin.websocket_connect("/agent/ws") as ws:
        ws.send_text(json.dumps({"type": "hello", "v": 1, "node": "pi", "token": token, "sensors": [],
                                 "features": ["vpn"]}))
        assert json.loads(ws.receive_text())["type"] == "welcome"
        assert json.loads(ws.receive_text())["type"] == "vpn"


def test_subscription_in_use_cannot_be_deleted(admin, db, subscription):
    n, _ = _node(db, [])
    key = vpn.unpack(subscription.servers_enc)[0]["key"]
    admin.post("/vpn/assign", data={"node_id": n.id, "server": f"{subscription.id}:{key}"})
    assert admin.post(f"/vpn/subscriptions/{subscription.id}/delete").status_code == 409
    assert not db.scalar(select(SettingValue).where(SettingValue.value.like("%token-abc%")))


def test_assign_unsupported_transport_is_400(admin, db):
    # kcp не умеют ни sing-box, ни Xray — понятная ошибка 400, а не 500
    link = f"vless://{UUID}@192.0.2.20:443?type=kcp&security=reality&pbk=PUBKEYexample123&sid=ab#KCP"
    sub = VpnSubscription(name="x", url_enc="", servers_enc=vpn.pack([vpn.parse_link(link)]), count=1)
    db.add(sub)
    db.commit()
    n, _ = _node(db, ["vpn"])
    key = vpn.unpack(sub.servers_enc)[0]["key"]
    r = admin.post("/vpn/assign", data={"node_id": n.id, "server": f"{sub.id}:{key}"})
    assert r.status_code == 400 and "kcp" in r.text
    assert db.scalar(select(VpnAssignment)) is None


def test_singbox_config_1_14_dns_format():
    servers, _ = vpn.parse(BODY)
    cfg = vpn.singbox_config(servers[0], [])
    assert cfg["dns"]["servers"] == [{"type": "tls", "tag": "remote", "server": "1.1.1.1", "detour": "vpn"},
                                     {"type": "local", "tag": "local"}]
    assert cfg["dns"]["rules"] == [{"domain": [servers[0]["host"]], "server": "local"}]
    assert cfg["dns"]["final"] == "remote" and cfg["route"]["default_domain_resolver"] == "local"
    assert all("address" not in s for s in cfg["dns"]["servers"])


def _server_node(db, features):
    from app.models import Server
    srv = Server(name="S", host="192.0.2.50", collector="agent")
    db.add(srv)
    db.commit()
    n, _ = _node(db, features)
    n.server_id = srv.id
    db.commit()
    return srv, n


def test_server_vpn_fragment_states(admin, db, subscription):
    from app.models import Server
    lone = Server(name="L", host="192.0.2.51", collector="agent")
    db.add(lone)
    db.commit()
    r = admin.get(f"/servers/{lone.id}/vpn")
    assert r.status_code == 200 and 'id="vpn"' in r.text and "установите агента" in r.text
    srv, n = _server_node(db, [])
    assert "VPN не разрешён на узле" in admin.get(f"/servers/{srv.id}/vpn").text
    n.info_json = json.dumps({"features": ["vpn"], "vpn": {"singbox": "1.14.4", "setup": True}})
    db.commit()
    text = admin.get(f"/servers/{srv.id}/vpn").text
    assert "<select" in text and "Нидерланды" in text and "1.14.4" in text and "Выключить" not in text
    key = vpn.unpack(subscription.servers_enc)[0]["key"]
    r = admin.post("/vpn/assign", data={"node_id": n.id, "server": f"{subscription.id}:{key}",
                                        "back": f"/servers/{srv.id}/settings#vpn"}, follow_redirects=False)
    assert r.headers["location"] == f"/servers/{srv.id}/settings#vpn"
    text = admin.get(f"/servers/{srv.id}/vpn").text
    assert "Выключить" in text and "ждёт узла" in text
    assert not any(s in text for s in SECRETS) and "token-abc" not in text


def test_server_vpn_fragment_permissions(user, viewer, db):
    srv, _ = _server_node(db, ["vpn"])
    assert user.get(f"/servers/{srv.id}/vpn").status_code == 403
    assert viewer.get(f"/servers/{srv.id}/vpn").status_code == 403


@pytest.mark.parametrize("back,expected", [
    ("/nodes/3#vpn", "/nodes/3#vpn"), ("/servers/3/settings", "/servers/3/settings"),
    ("https://evil", "/vpn#nodes"), ("//evil", "/vpn#nodes"), ("/servers/3/settings#x", "/vpn#nodes"),
    ("/nodes/3/../../evil", "/vpn#nodes"),
])
def test_assign_back_only_local(admin, db, subscription, back, expected):
    n, _ = _node(db, ["vpn"])
    key = vpn.unpack(subscription.servers_enc)[0]["key"]
    r = admin.post("/vpn/assign", data={"node_id": n.id, "server": f"{subscription.id}:{key}", "back": back},
                   follow_redirects=False)
    assert r.headers["location"] == expected
    r = admin.post(f"/vpn/assign/{n.id}/off", data={"back": back}, follow_redirects=False)
    assert r.headers["location"] == expected


def test_vpn_page_links_to_node_settings(admin, db, subscription):
    srv, n = _server_node(db, ["vpn"])
    assert f"/servers/{srv.id}/settings#vpn" in admin.get("/vpn").text


def test_diagnose_reality_without_secrets():
    """vpn-check: форма ключей reality без самих ключей; испорченный pbk помечен «!»."""
    bad = vpn.parse_link(VLESS)
    lines = "\n".join(vpn.diagnose(bad))
    assert "PUBKEYexample123" not in lines and UUID not in lines
    assert "pbk: 16 симв." in lines and "! ключ должен быть 32 байта" in lines
    assert "sni: www.microsoft.com" in lines and "flow: xtls-rprx-vision" in lines
    good_key = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    good = vpn.parse_link(VLESS.replace("PUBKEYexample123", good_key).replace("sid=abcd", "sid=xyz"))
    lines = "\n".join(vpn.diagnose(good))
    assert good_key not in lines and "32 байт" in lines and "! ключ" not in lines
    assert "! short id" in lines


def test_assign_fingerprint_override(admin, db, subscription):
    """Отпечаток uTLS из настроек узла заменяет указанный в подписке; неизвестный — 400."""
    n, _ = _node(db, ["vpn"])
    key = vpn.unpack(subscription.servers_enc)[0]["key"]
    data = {"node_id": n.id, "server": f"{subscription.id}:{key}"}
    assert admin.post("/vpn/assign", data={**data, "fingerprint": "<x>"}).status_code == 400
    admin.post("/vpn/assign", data={**data, "fingerprint": "firefox"})
    a = db.scalar(select(VpnAssignment))
    tls = vpn_router.message_for(db, a)["config"]["outbounds"][0]["tls"]
    assert a.fingerprint == "firefox" and tls["utls"]["fingerprint"] == "firefox" and tls["reality"]["enabled"]
    admin.post("/vpn/assign", data=data)  # «как в подписке» (в тестовой ссылке — chrome)
    db.expire_all()
    a = db.scalar(select(VpnAssignment))
    assert a.fingerprint == "" and vpn_router.message_for(db, a)["config"]["outbounds"][0]["tls"]["utls"]["fingerprint"] == "chrome"
    srv = db.scalar(select(Node).where(Node.id == n.id)).server_id
    if srv:
        assert "Отпечаток: как в подписке" in admin.get(f"/servers/{srv}/vpn").text


XHTTP = (f"vless://{UUID}@192.0.2.20:443?type=xhttp&security=reality&sni=www.example.org&fp=chrome"
         f"&pbk=PUBKEYexample123&sid=ab&spx=%2Fx&path=%2Fup&host=cdn.example.org&mode=packet-up"
         f"&extra=%7B%22noSSEHeader%22%3Atrue%7D#XHTTP")


def test_parse_xhttp_params():
    s = vpn.parse_link(XHTTP)
    assert s["transport"] == "xhttp" and s["mode"] == "packet-up" and s["spider_x"] == "/x"
    assert s["extra"] == {"noSSEHeader": True} and s["path"] == "/up" and s["host_header"] == "cdn.example.org"
    for junk in ("%7Bbroken", "%5B1%5D", "123"):  # не JSON и не объект — игнор
        assert vpn.parse_link(XHTTP.replace("%7B%22noSSEHeader%22%3Atrue%7D", junk))["extra"] == {}


def test_xhttp_only_xray():
    cfg, xr = vpn.engine_configs(vpn.parse_link(XHTTP))
    assert cfg is None and xr
    proxy = xr["xray"]["outbounds"][0]
    st = proxy["streamSettings"]
    assert st["network"] == "xhttp" and st["xhttpSettings"] == {"path": "/up", "host": "cdn.example.org",
                                                                 "mode": "packet-up", "extra": {"noSSEHeader": True}}
    assert st["realitySettings"] == {"serverName": "www.example.org", "fingerprint": "chrome",
                                     "publicKey": "PUBKEYexample123", "shortId": "ab", "spiderX": "/x"}
    assert proxy["settings"]["vnext"][0]["users"][0]["id"] == UUID


def test_reality_tcp_both_engines_and_structure():
    cfg, xr = vpn.engine_configs(vpn.parse_link(VLESS), "firefox")
    assert cfg["outbounds"][0]["tls"]["utls"]["fingerprint"] == "firefox"
    assert xr["xray"]["outbounds"][0]["streamSettings"]["realitySettings"]["fingerprint"] == "firefox"
    x = xr["xray"]
    assert len(x["inbounds"]) == 1
    ib = x["inbounds"][0]
    assert (ib["protocol"], ib["listen"], ib["port"]) == ("socks", "127.0.0.1", 10808)
    assert ib["settings"]["udp"] is True and ib["settings"]["auth"] == "noauth"
    assert [(o["tag"], o["protocol"]) for o in x["outbounds"]] == [("proxy", "vless"), ("direct", "freedom")]
    assert x["outbounds"][0]["settings"]["vnext"][0]["users"][0]["flow"] == "xtls-rprx-vision"
    sb = xr["singbox"]
    assert sb["outbounds"][0] == {"type": "socks", "tag": "vpn", "server": "127.0.0.1", "server_port": 10808,
                                  "version": "5"}
    assert sb["inbounds"][0]["type"] == "tun" and "10.0.0.0/8" in sb["inbounds"][0]["route_exclude_address"]
    assert sb["route"]["final"] == "vpn" and sb["route"]["auto_detect_interface"] is True


def test_other_protocols_xray():
    for link in (VMESS, TROJAN, SS_NEW):
        xr = vpn.xray_config(vpn.parse_link(link))
        assert xr["xray"]["outbounds"][0]["tag"] == "proxy"


def test_hysteria2_only_singbox():
    cfg, xr = vpn.engine_configs(vpn.parse_link(HY2))
    assert cfg and xr is None
    assert vpn.engine_support(vpn.parse_link(HY2)) == (True, False)


def _assign_msg(admin, db, link, fingerprint=""):
    sub = VpnSubscription(name="x", url_enc="", servers_enc=vpn.pack([vpn.parse_link(link)]), count=1)
    db.add(sub)
    db.commit()
    n, _ = _node(db, ["vpn"])
    key = vpn.unpack(sub.servers_enc)[0]["key"]
    admin.post("/vpn/assign", data={"node_id": n.id, "server": f"{sub.id}:{key}", "fingerprint": fingerprint})
    return n, vpn_router.message_for(db, db.scalar(select(VpnAssignment)))


def test_message_xhttp_and_fingerprint(admin, db):
    _, msg = _assign_msg(admin, db, XHTTP, "safari")
    assert msg["config"] is None
    assert msg["xray"]["xray"]["outbounds"][0]["streamSettings"]["realitySettings"]["fingerprint"] == "safari"
    assert "uuid" not in msg["server"]


def test_engine_saved_from_result(admin, db):
    n, msg = _assign_msg(admin, db, VLESS)
    assert msg["config"] and msg["xray"]
    for engine, expected in (("xray", "xray"), ("sing-box", "sing-box"), ("evil<b>", ""), (None, "")):
        vpn_router.on_result(db, n, {"id": msg["id"], "ok": True, "detail": "ok", "engine": engine})
        db.expire_all()
        assert db.scalar(select(VpnAssignment)).engine == expected


def test_secrets_not_in_html_or_diagnose(admin, db):
    _assign_msg(admin, db, XHTTP)
    page = admin.get("/vpn").text
    lines = "\n".join(vpn.diagnose(vpn.parse_link(XHTTP)))
    for secret in (UUID, "PUBKEYexample123"):
        assert secret not in page and secret not in lines
    assert "mode: packet-up" in lines and "движки: sing-box нет, xray да" in lines


def test_vpn_log_events_masked_and_interfaces_refreshed(admin, db):
    from app.models import NodeEvent, Server
    n, msg = _assign_msg(admin, db, VLESS)
    srv = Server(name="S2", host="192.0.2.51", collector="agent")
    db.add(srv)
    db.commit()
    n.server_id = srv.id
    db.commit()
    uuid = "12345678-1234-1234-1234-1234567890ab"
    vpn_router.on_result(db, n, {"id": msg["id"], "ok": False, "detail": f"Xray отверг: bad id {uuid}", "engine": ""})
    vpn_router.on_result(db, n, {"id": msg["id"], "ok": True, "detail": "", "engine": "xray",
                                 "interfaces": [{"name": "hl-vpn", "mac": "", "ipv4": ["172.19.0.1"], "ipv6": [],
                                                 "up": True, "speed": 0}, {"name": "bad name!"}]})
    db.expire_all()
    texts = [e.text for e in db.scalars(select(NodeEvent).order_by(NodeEvent.id))]
    assert any(t.startswith("VPN: сервер «") for t in texts)  # назначение (узел не на связи — ждёт)
    assert any("откатился" in t for t in texts) and not any(uuid in t for t in texts)
    assert texts[-1].startswith("VPN: включён") and "xray" in texts[-1]
    node = db.get(Node, n.id)
    assert [i["name"] for i in node.info["interfaces"]] == ["hl-vpn"]
    page = admin.get(f"/servers/{srv.id}/vpn").text
    assert "Журнал подключения" in page and "откатился" in page and uuid not in page
    assert "VPN-туннель" in admin.get(f"/servers/{srv.id}").text
