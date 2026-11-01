"""Удаление в одном месте — везде, архив с историей (14 дней) и журнал безопасности."""
import gzip
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (Archive, Device, Link, LoginAttempt, Node, NodeSample, SecurityEvent, Server,
                        ServerMetric, Service)
from app.services import audit, infra
from app.services.security import encrypt
from app.utils import utcnow


@pytest.fixture
def bundle(db):
    """Сервер с сервисом, историей, узлом-агентом и местом на карте (со связью к свитчу)."""
    switch = Device(name="Свитч", type="switch")
    server = Server(name="Pi", host="192.0.2.10", collector="agent", ssh_key_enc=encrypt("/home/x/.ssh/id"))
    db.add_all([switch, server])
    db.flush()
    device = Device(name="Pi", type="server", ip="192.0.2.10", server_id=server.id)
    node = Node(name="pi", token_hash="h" * 64, server_id=server.id, values_json='{"cpu.percent": 5}')
    db.add_all([device, node, Service(server_id=server.id, name="ssh", check_type="tcp", target="192.0.2.10:22")])
    db.flush()
    db.add_all([Link(a_id=switch.id, b_id=device.id),
                ServerMetric(server_id=server.id, online=True, ssh_ok=None, ram_total=1, ram_used=1, ram_free=0,
                             ts=utcnow() - timedelta(days=3)),
                NodeSample(node_id=node.id, ts=utcnow() - timedelta(hours=2), values_json='{"cpu.percent": 7}')])
    db.commit()
    return server, device, node, switch


def _gone(db, ids):
    db.expire_all()
    assert db.get(Server, ids[0]) is None and db.get(Device, ids[1]) is None and db.get(Node, ids[2]) is None
    assert db.scalar(select(func.count()).select_from(Link)) == 0
    assert db.scalar(select(func.count()).select_from(Service)) == 0


@pytest.mark.parametrize("where", ["servers", "nodes", "topology"])
def test_delete_anywhere_removes_everywhere(admin, db, bundle, where):
    ids = [obj.id for obj in bundle]
    url = {"servers": f"/servers/{ids[0]}/delete", "nodes": f"/nodes/{ids[2]}/delete",
           "topology": f"/topology/devices/{ids[1]}/delete"}[where]
    assert admin.post(url, follow_redirects=False).status_code == 303
    _gone(db, ids)
    assert db.get(Device, ids[3]) is not None  # соседнее устройство остаётся

    archive = db.scalar(select(Archive))
    assert archive.kind == "server" and archive.name == "Pi" and archive.deleted_by == "admin"
    data = json.loads(gzip.decompress(archive.data))
    assert data["server"]["services"][0]["name"] == "ssh" and len(data["server"]["history"]) == 1
    assert data["nodes"][0]["history"][0]["values"] == {"cpu.percent": 7}
    assert data["devices"][0]["links"][0]["a"] == "Свитч"
    raw = gzip.decompress(archive.data).decode()
    assert "id_" not in raw and "token" not in raw and "/home/x" not in raw  # ни ключа, ни хеша токена

    page = admin.get("/archive").text
    assert "Pi" in page and "узел с агентом: pi" in page
    r = admin.get(f"/archive/{archive.id}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    assert "attachment" in r.headers["content-disposition"] and r.json()["server"]["name"] == "Pi"
    kinds = {e.kind for e in db.scalars(select(SecurityEvent))}
    assert {"delete", "archive_download"} <= kinds


def test_confirm_text_lists_everything(admin, db, bundle):
    server = bundle[0]
    text = admin.get(f"/servers/{server.id}/settings", follow_redirects=True).text  # → настройки на странице узла
    assert "узел с агентом «pi»" in text and "«Pi» на карте сети" in text and "Удалить везде" in text


def test_plain_device_and_node_delete_alone(admin, db):
    d = Device(name="Свитч", type="switch")
    n = Node(name="solo", token_hash="x" * 64)
    db.add_all([d, n])
    db.commit()
    d_id, n_id = d.id, n.id
    admin.post(f"/topology/devices/{d_id}/delete")
    admin.post(f"/nodes/{n_id}/delete")
    db.expire_all()
    assert db.get(Device, d_id) is None and db.get(Node, n_id) is None
    assert [a.kind for a in db.scalars(select(Archive))] == ["node"]  # устройство без сервера не архивируется


def test_server_device_delete_needs_servers_edit(user, db, bundle):
    """Право на карту без права на серверы не даёт удалить сервер через карту."""
    from app.models import Group, GroupPermission

    group = db.scalar(select(Group).where(Group.name == "Пользователи"))
    db.query(GroupPermission).filter(GroupPermission.group_id == group.id,
                                     GroupPermission.perm == "servers.edit").delete()
    db.commit()
    server_id, device_id = bundle[0].id, bundle[1].id
    assert user.post(f"/topology/devices/{device_id}/delete").status_code == 403
    db.expire_all()
    assert db.get(Server, server_id) is not None


def test_viewer_can_download_but_not_delete(viewer, admin, db, bundle):
    admin.post(f"/servers/{bundle[0].id}/delete")
    archive = db.scalar(select(Archive))
    assert viewer.get(f"/archive/{archive.id}/download").status_code == 200
    assert viewer.post(f"/archive/{archive.id}/delete").status_code == 403
    assert admin.post(f"/archive/{archive.id}/delete", follow_redirects=False).status_code == 303
    assert db.scalar(select(Archive)) is None


def test_history_download_live(admin, db, bundle):
    server, _, node, _ = bundle
    r = admin.get(f"/servers/{server.id}/history.json")
    assert r.status_code == 200 and r.json()["server"]["history"] and r.json()["nodes"][0]["name"] == "pi"
    assert admin.get(f"/nodes/{node.id}/history.json").json()["nodes"][0]["history"]


def test_retention_cleans_history_and_archive(db, bundle):
    server = bundle[0]
    db.add(ServerMetric(server_id=server.id, online=True, ts=utcnow() - timedelta(days=20)))
    db.add(Archive(kind="node", name="old", data=gzip.compress(b"{}"), deleted_at=utcnow() - timedelta(days=15)))
    db.add(Archive(kind="node", name="fresh", data=gzip.compress(b"{}"), deleted_at=utcnow() - timedelta(days=13)))
    db.commit()
    infra.cleanup(db)
    assert [a.name for a in db.scalars(select(Archive))] == ["fresh"]
    assert db.scalar(select(func.count()).select_from(ServerMetric)) == 1  # осталась 3-дневная


# ---------- Журнал безопасности ----------

def test_security_page_and_events(admin, client, db):
    client.post("/login", data={"username": "admin", "password": "wrong", "csrf_token": "x"})  # без CSRF — 403
    r = client.get("/login")
    import re
    token = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    client.post("/login", data={"username": "admin", "password": "wrong", "csrf_token": token})
    page = admin.get("/settings/security").text
    assert "Неудачных входов за 24 ч" in page and "неверные данные" in page and "это вы" in page
    assert "admin" in admin.get("/settings/security?fails=1&q=admin").text
    admin.post("/logout")
    assert db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "logout")).username == "admin"


def test_security_page_only_users_manage(user, viewer):
    for c in (user, viewer):
        assert c.get("/settings/security").status_code == 403
        assert c.get("/settings/security/log.csv").status_code == 403


def test_sessions_revoke(admin, user, db):
    from app.models import UserSession

    other = db.scalar(select(UserSession).join(UserSession.user).where(UserSession.user.has(username="user")))
    assert admin.post(f"/settings/security/sessions/{other.id}/revoke", follow_redirects=False).status_code == 303
    assert user.get("/", follow_redirects=False).status_code in (302, 303, 307)  # выкинуло на вход
    admin.post("/settings/security/sessions/revoke-others")
    assert admin.get("/").status_code == 200  # своя сессия цела


def test_csv_export_escapes_formulas(admin, db):
    audit.log_event(db, "delete", username="admin", detail="=HYPERLINK(\"http://evil\")")
    r = admin.get("/settings/security/log.csv")
    assert r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf")
    assert "'=HYPERLINK" in r.text


def test_security_retention(db):
    old = utcnow() - timedelta(days=91)
    db.add(LoginAttempt(username="x", ip="1.1.1.1", success=False, created_at=old))
    db.add(SecurityEvent(kind="logout", ts=old))
    db.add(SecurityEvent(kind="logout"))
    db.commit()
    audit.cleanup(db)
    assert db.scalar(select(func.count()).select_from(LoginAttempt)) == 0
    assert db.scalar(select(func.count()).select_from(SecurityEvent)) == 1


def test_agent_auth_logged(admin, client, db):
    with client.websocket_connect("/agent/ws") as ws:
        ws.send_text(json.dumps({"type": "hello", "node": "ghost", "token": "nope", "sensors": []}))
        ws.receive_text()
    ev = db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "agent_auth"))
    assert ev.ok is False and ev.username == "ghost" and ev.detail == "bad token"
