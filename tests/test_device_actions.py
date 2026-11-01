"""Действия над устройствами карты сети: MAC, реестр по типам, Wake-on-LAN через агента, права."""
import json
import threading

import pytest
from sqlalchemy import select

from app.models import Device, Node, SecurityEvent, Server
from app.services import device_actions as da
from app.services.security import hash_token

MAC = "aa:bb:cc:dd:ee:ff"


def _device(db, **kw):
    d = Device(**{"name": "ПК", "type": "pc", "ip": "192.168.1.29", "mac": MAC, **kw})
    db.add(d)
    db.commit()
    return d


def _node(db, features, name="pi"):
    token = "tok-" + name * 10 + "x" * 20
    db.add(Node(name=name, token_hash=hash_token(token), info_json=json.dumps({"features": features})))
    db.commit()
    return token


def _hello(ws, token, name="pi", features=("wol",)):
    ws.send_text(json.dumps({"type": "hello", "v": 1, "node": name, "token": token, "sensors": [],
                             "features": list(features)}))
    assert json.loads(ws.receive_text())["type"] == "welcome"


def test_normalize_mac():
    assert da.normalize_mac("AA:BB:CC:DD:EE:FF") == MAC
    assert da.normalize_mac("aa-bb-cc-dd-ee-ff") == MAC
    assert da.normalize_mac("  ") == ""
    for bad in ("aabbccddeeff", "aa:bb:cc", "zz:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff:00"):
        with pytest.raises(ValueError):
            da.normalize_mac(bad)


def test_mac_saved_and_validated(admin, db):
    form = {"name": "Игровой", "type": "pc", "ip": "10.0.0.5", "mac": "AA:BB:CC:DD:EE:0A"}
    assert admin.post("/topology/devices", data=form, follow_redirects=False).status_code == 303
    assert db.scalar(select(Device).where(Device.name == "Игровой")).mac == "aa:bb:cc:dd:ee:0a"
    assert admin.post("/topology/devices", data={**form, "mac": "oops"}).status_code == 400


def test_registry_by_type(admin, db):
    pc = _device(db)
    router = _device(db, name="R", type="router", mac="")
    cloud = _device(db, name="Net", type="cloud", ip="", mac="")
    ids = lambda d: [a["id"] for a in da.available(db, d, _user(db))]  # noqa: E731
    assert ids(pc) == ["web", "wol"]
    assert ids(router) == ["web"]
    assert ids(cloud) == []
    assert da.available(db, pc, _user(db))[0]["href"] == "http://192.168.1.29"
    # Без MAC Wake-on-LAN нет; с привязанным сервером появляется страница сервера
    pc.mac = ""
    pc.server = Server(name="S", host="1.2.3.4", collector="ssh", enabled=False)
    db.commit()
    assert ids(pc) == ["web", "server_page"]


def _user(db):
    from app.models import User
    u = db.scalar(select(User).where(User.username == "admin")) or db.scalars(select(User)).first()
    return u


def test_api_exposes_actions_by_permission(admin, viewer, db):
    d = _device(db)
    node = next(n for n in admin.get("/api/topology").json()["nodes"] if n["id"] == d.id)
    assert {a["id"] for a in node["actions"]} == {"web", "wol"} and node["mac"] == MAC
    node = next(n for n in viewer.get("/api/topology").json()["nodes"] if n["id"] == d.id)
    assert {a["id"] for a in node["actions"]} == {"web"} and node["mac"] == ""


def test_wol_no_agent_clear_error(admin, db):
    d = _device(db)
    r = admin.post(f"/topology/devices/{d.id}/actions/wol")
    assert r.status_code == 409 and "агента" in r.json()["detail"]
    ev = db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "device_action"))
    assert ev and not ev.ok


def test_wol_permissions_and_bad_requests(admin, user, viewer, db):
    d = _device(db)
    router = _device(db, name="R", type="router")
    assert viewer.post(f"/topology/devices/{d.id}/actions/wol").status_code == 403
    assert admin.post(f"/topology/devices/{router.id}/actions/wol").status_code == 404
    assert admin.post(f"/topology/devices/{d.id}/actions/web").status_code == 404  # ссылка, не POST-действие
    assert admin.post(f"/topology/devices/{d.id}/actions/nope").status_code == 404
    assert admin.post("/topology/devices/9999/actions/wol").status_code == 404
    d.mac = ""
    db.commit()
    assert user.post(f"/topology/devices/{d.id}/actions/wol").status_code == 400


def test_wol_sent_to_agent(admin, db):
    d = _device(db)
    token = _node(db, ["wol"])
    result = {}
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws, token)
            t = threading.Thread(target=lambda: result.update(r=admin.post(f"/topology/devices/{d.id}/actions/wol")))
            t.start()
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "wol" and msg["mac"] == MAC and msg["broadcast"] == "255.255.255.255"
            assert msg["id"].startswith("w-")
            ws.send_text(json.dumps({"type": "wol_result", "id": msg["id"], "ok": True, "detail": "отправлен"}))
            t.join(10)
    assert result["r"].status_code == 200 and "pi" in result["r"].json()["message"]
    ev = db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "device_action"))
    assert ev and ev.ok and "ПК" in ev.detail


def test_wol_agent_refuses(admin, db):
    d = _device(db)
    token = _node(db, ["wol"])
    result = {}
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws, token)
            t = threading.Thread(target=lambda: result.update(r=admin.post(f"/topology/devices/{d.id}/actions/wol")))
            t.start()
            msg = json.loads(ws.receive_text())
            ws.send_text(json.dumps({"type": "wol_result", "id": msg["id"], "ok": False, "detail": "сеть: ошибка"}))
            t.join(10)
    assert result["r"].status_code == 409 and "сеть: ошибка" in result["r"].json()["detail"]


def test_agent_without_wol_feature_ignored(admin, db):
    d = _device(db)
    token = _node(db, [])
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws, token, features=())
            r = admin.post(f"/topology/devices/{d.id}/actions/wol")
    assert r.status_code == 409
