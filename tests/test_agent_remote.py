"""Управление агентом из панели: настройки (config_*) и удаление агента вместе с узлом (uninstall, надгробия)."""
import json
import threading
from datetime import timedelta

from sqlalchemy import func, select

from app.models import Node, NodeTombstone, SecurityEvent
from app.services import agent_remote, infra
from app.services.security import hash_token
from app.utils import utcnow

SECTIONS = {"interval": {"seconds": 10}, "wol": {"enabled": False}}


def _node(db, features, name="pi"):
    token = "tok-" + name * 10 + "x" * 20
    db.add(Node(name=name, token_hash=hash_token(token), info_json=json.dumps({"features": features})))
    db.commit()
    return token


def _hello(ws, token, name="pi", features=("config", "uninstall")):
    ws.send_text(json.dumps({"type": "hello", "v": 1, "node": name, "token": token, "sensors": [],
                             "features": list(features)}))
    assert json.loads(ws.receive_text())["type"] == "welcome"


def _node_id(db, name="pi"):
    return db.scalar(select(Node.id).where(Node.name == name))


def _post_async(client, url, data=None):
    result = {}
    t = threading.Thread(target=lambda: result.update(r=client.post(url, data=data or {}, follow_redirects=False)))
    t.start()
    return t, result


def test_config_get_and_set(admin, db):
    token = _node(db, ["config"])
    nid = _node_id(db)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws, token, features=("config",))
            t, res = _post_async(admin, f"/nodes/{nid}/config/get")
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "config_get" and msg["id"].startswith("c-")
            ws.send_text(json.dumps({"type": "config", "id": msg["id"], "managed": ["interval", "wol"],
                                     "sections": SECTIONS, "local_only": ["panel", "collectors.command"]}))
            t.join(10)
            page = res["r"].text
            assert res["r"].status_code == 200 and 'name="sec_interval"' in page and "collectors.command" in page

            t, res = _post_async(admin, f"/nodes/{nid}/config/set",
                                 {"sec_interval": '{"seconds": 30}', "sec_wol": '{"enabled": true}'})
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "config_set" and msg["sections"] == {"interval": {"seconds": 30},
                                                                       "wol": {"enabled": True}}
            ws.send_text(json.dumps({"type": "config_result", "id": msg["id"], "ok": True, "detail": "ок"}))
            t.join(10)
    assert res["r"].status_code == 200 and "перезапускается" in res["r"].text
    assert db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "agent_config")).ok


def test_config_set_refused_by_agent(admin, db):
    token = _node(db, ["config"])
    nid = _node_id(db)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws, token, features=("config",))
            t, res = _post_async(admin, f"/nodes/{nid}/config/set", {"sec_interval": '{"seconds": -1}'})
            msg = json.loads(ws.receive_text())
            ws.send_text(json.dumps({"type": "config_result", "id": msg["id"], "ok": False, "detail": "interval < 1"}))
            t.join(10)
    assert res["r"].status_code == 409 and "interval &lt; 1" in res["r"].text
    assert not db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "agent_config")).ok


def test_config_panel_validation(admin, db):
    token = _node(db, ["config"])
    nid = _node_id(db)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws, token, features=("config",))
            for bad in ({"sec_interval": "не json"}, {"sec_interval": "[1]"}, {"sec_panel": "{}"},
                        {"sec_collectors": '{"command": "rm"}'}, {"sec_interval": "{" + " " * 25000 + "}"}, {}):
                r = admin.post(f"/nodes/{nid}/config/set", data=bad)
                assert r.status_code == 409, bad


def test_config_needs_feature_and_online(admin, db):
    _node(db, [])
    nid = _node_id(db)
    r = admin.post(f"/nodes/{nid}/config/get")
    assert r.status_code == 409 and "не сообщил" in r.text
    _node(db, ["config"], name="off")
    r = admin.post(f"/nodes/{_node_id(db, 'off')}/config/get")
    assert r.status_code == 409 and "не на связи" in r.text


def test_config_permissions(viewer, admin, db):
    _node(db, ["config"])
    nid = _node_id(db)
    assert viewer.post(f"/nodes/{nid}/config/get").status_code == 403
    assert viewer.post(f"/nodes/{nid}/config/set", data={"sec_interval": "{}"}).status_code == 403


def test_delete_online_node_sends_uninstall(admin, db):
    token = _node(db, ["uninstall"])
    nid = _node_id(db)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws, token, features=("uninstall",))
            t, res = _post_async(admin, f"/nodes/{nid}/delete", {"remove_agent": "1"})
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "uninstall" and msg["id"].startswith("x-")
            ws.send_text(json.dumps({"type": "uninstall_result", "id": msg["id"], "ok": True, "detail": "ok"}))
            t.join(15)
    assert res["r"].status_code == 303
    db.expire_all()
    assert db.get(Node, nid) is None
    assert db.scalar(select(func.count()).select_from(NodeTombstone)) == 0
    assert db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "agent_uninstall"))


def test_delete_offline_makes_tombstone_then_hello_gets_uninstall(admin, db, client):
    token = _node(db, ["uninstall"])
    nid = _node_id(db)
    assert admin.post(f"/nodes/{nid}/delete", data={"remove_agent": "1"}, follow_redirects=False).status_code == 303
    tomb = db.scalar(select(NodeTombstone))
    assert tomb and tomb.name == "pi" and tomb.token_hash == hash_token(token) and token not in tomb.token_hash
    assert timedelta(days=29) < tomb.expires_at - utcnow() <= timedelta(days=30)
    with client.websocket_connect("/agent/ws") as ws:
        ws.send_text(json.dumps({"type": "hello", "v": 1, "node": "pi", "token": token, "sensors": []}))
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "uninstall"
        ws.send_text(json.dumps({"type": "uninstall_result", "id": msg["id"], "ok": True, "detail": "ok"}))
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(NodeTombstone)) == 0


def test_unknown_token_still_bad_token(client, db):
    with client.websocket_connect("/agent/ws") as ws:
        ws.send_text(json.dumps({"type": "hello", "v": 1, "node": "ghost", "token": "z" * 40, "sensors": []}))
        assert json.loads(ws.receive_text())["type"] == "error"


def test_tombstone_expires(db, client):
    token = "old-" + "x" * 30
    db.add(NodeTombstone(name="old", token_hash=hash_token(token), expires_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    assert agent_remote.find_tombstone(db, token) is None  # просроченное уже не срабатывает
    infra.cleanup(db)
    assert db.scalar(select(func.count()).select_from(NodeTombstone)) == 0


def test_checkbox_off_no_tombstone(admin, db):
    _node(db, ["uninstall"])
    nid = _node_id(db)
    admin.post(f"/nodes/{nid}/delete", data={"remove_agent": "0"}, follow_redirects=False)
    db.expire_all()
    assert db.get(Node, nid) is None
    assert db.scalar(select(func.count()).select_from(NodeTombstone)) == 0
