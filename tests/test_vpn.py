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
    cfg = vpn.singbox_config(by["vless"], bypass=["192.168.1.39"])
    out = cfg["outbounds"][0]
    assert out["type"] == "vless" and out["tls"]["reality"]["public_key"] == "PUBKEYexample123"
    tun = cfg["inbounds"][0]
    assert tun["auto_route"] and "192.168.0.0/16" in tun["route_exclude_address"]
    assert "192.168.1.39/32" in tun["route_exclude_address"]
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
