"""Узлы и приём агентов: токены, протокол v1, метрики для «Серверов», действия восстановления."""
import asyncio
import json
import re
import time

import pytest
from sqlalchemy import select

from app.models import Device, Node, NodeAction, NodeSample, Server
from app.services import agents, monitoring
from app.services.security import hash_token

HELLO = {
    "type": "hello", "v": 1, "node": "pi", "version": "0.1.0", "ip": ["192.0.2.10"],
    "info": {"hostname": "pi", "os": "Debian 12", "threads": 4},
    "sensors": [
        {"id": "memory.total", "kind": "memory", "label": "RAM", "unit": "B"},
        {"id": "memory.used", "kind": "memory", "label": "RAM занято", "unit": "B"},
        {"id": "memory.available", "kind": "memory", "label": "RAM доступно", "unit": "B"},
        {"id": "disks./.total", "kind": "disk", "label": "/dev/mmcblk0p2 ext4", "unit": "B"},
        {"id": "disks./.used", "kind": "disk", "label": "/dev/mmcblk0p2 ext4", "unit": "B"},
        {"id": "disks./.free", "kind": "disk", "label": "/dev/mmcblk0p2 ext4", "unit": "B"},
        {"id": "thermal.cpu.temp", "kind": "temperature", "label": "CPU", "unit": "°C"},
        {"id": "cpu.percent", "kind": "load", "label": "CPU", "unit": "%"},
    ],
    "actions": [{"name": "restart_nginx", "label": "Перезапустить nginx"}],
}
VALUES = {"memory.total": 1_000_000_000, "memory.used": 400_000_000, "memory.available": 600_000_000,
          "disks./.total": 31_000_000_000, "disks./.used": 11_000_000_000, "disks./.free": 18_500_000_000,
          "thermal.cpu.temp": 51.5, "cpu.percent": 12.0}


def _create(admin, name="pi", **extra) -> str:
    r = admin.post("/nodes", data={"name": name, "server_id": "", **extra})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    return re.search(r'readonly value="([^"]+)"', r.text).group(1)


def _hello(token: str, **over) -> str:
    return json.dumps({**HELLO, "token": token, **over})


def test_create_node_shows_token_once_and_stores_hash(admin, db):
    token = _create(admin)
    node = db.scalar(select(Node).where(Node.name == "pi"))
    assert node.token_hash == hash_token(token) and token not in node.token_hash
    assert token not in admin.get(f"/nodes/{node.id}").text
    assert token not in admin.get("/api/nodes").text
    assert admin.post("/nodes", data={"name": "pi"}).status_code == 400  # дубль
    assert admin.post("/nodes", data={"name": "bad name!"}).status_code == 400


def test_nodes_permissions(viewer, admin, db):
    assert viewer.get("/nodes").status_code == 200
    assert viewer.post("/nodes", data={"name": "x"}).status_code == 403
    assert "Новый узел" not in viewer.get("/nodes").text
    token = _create(admin)
    node = db.scalar(select(Node))
    assert viewer.post(f"/nodes/{node.id}/token").status_code == 403
    assert viewer.post(f"/nodes/{node.id}/actions/restart_nginx").status_code == 403
    assert token


def test_bad_token_rejected(admin, client):
    _create(admin)
    with client.websocket_connect("/agent/ws") as ws:
        ws.send_text(_hello("wrong"))
        assert json.loads(ws.receive_text()) == {"type": "error", "message": "bad token"}
    with client.websocket_connect("/agent/ws") as ws:
        ws.send_text(json.dumps({"type": "sample"}))
        assert json.loads(ws.receive_text())["type"] == "error"


def test_agent_session_feeds_node_and_server(admin, db):
    token = _create(admin, server_id="new", host="192.0.2.10", on_map="on")
    node = db.scalar(select(Node))
    server = db.scalar(select(Server))
    assert node.server_id == server.id and server.collector == "agent"
    assert db.scalar(select(Device)).server_id == server.id

    with admin:  # общий event loop для WebSocket и HTTP-запросов
        with admin.websocket_connect("/agent/ws") as ws:
            ws.send_text(_hello(token))
            welcome = json.loads(ws.receive_text())
            assert welcome["type"] == "welcome" and welcome["interval_ms"] == 10_000
            ws.send_text(json.dumps({"type": "sample", "ts": int(time.time() * 1000), "values": VALUES}))
            ws.send_text(json.dumps({"type": "event", "level": "warning", "text": "nginx упал"}))
            # Ответ на sample не приходит — ждём, пока панель его обработает
            for _ in range(50):
                db.expire_all()
                if db.get(Node, node.id).values_ts:
                    break
                time.sleep(0.05)

            # Информация — на странице сервера; на странице узла только настройки и действия
            page = admin.get(f"/servers/{server.id}").text
            assert "Онлайн" in page and "28.9 ГБ" in page and "nginx упал" in page
            assert "nginx упал" not in admin.get(f"/nodes/{node.id}").text
            assert "Перезапустить nginx" in admin.get(f"/nodes/{node.id}").text

            # Действие: панель шлёт только имя, агент отвечает результатом
            assert admin.post(f"/nodes/{node.id}/actions/rm_rf").status_code == 409
            assert admin.post(f"/nodes/{node.id}/actions/restart_nginx", follow_redirects=False).status_code == 303
            msg = json.loads(ws.receive_text())
            assert msg == {"type": "action", "id": msg["id"], "name": "restart_nginx"}
            ws.send_text(json.dumps({"type": "action_result", "id": msg["id"], "name": "restart_nginx",
                                     "ok": True, "code": 0, "output": "done"}))
            for _ in range(50):
                db.expire_all()
                action = db.scalar(select(NodeAction))
                if action.status != "sent":
                    break
                time.sleep(0.05)
            assert action.status == "ok" and action.output == "done"

        db.expire_all()
        assert db.get(Node, node.id).connected is False  # соединение закрыто

    # «Серверы» берут RAM и диски с агента — настоящие значения, не контейнерные точки монтирования
    db.expire_all()
    n = db.get(Node, node.id)
    n.connected = True  # как будто агент ещё на связи
    db.commit()
    metric = asyncio.run(monitoring.check_server(db, db.get(Server, server.id)))
    assert metric.online and metric.ram_total == 1_000_000_000 and metric.ram_used == 400_000_000
    assert json.loads(metric.disks) == [{"mount": "/", "size": 31_000_000_000, "used": 11_000_000_000,
                                         "free": 18_500_000_000, "device": "/dev/mmcblk0p2 ext4"}]
    assert "/dev/mmcblk0p2 ext4" in admin.get(f"/servers/{server.id}").text
    hist = admin.get(f"/api/nodes/{node.id}/history").json()
    assert hist["points"] and {s["kind"] for s in hist["sensors"]} >= {"temperature", "memory", "disk"}


def test_agent_server_without_data_shows_nothing(db):
    server = Server(name="x", host="10.0.0.1", collector="agent")
    db.add(server)
    db.commit()
    metric = asyncio.run(monitoring.check_server(db, server))
    assert metric.online is False and json.loads(metric.disks) == [] and metric.ram_total is None
    assert "не привязан" in server.last_error


def test_server_metrics_parses_mounts_with_dots():
    m = agents.server_metrics({"disks./mnt/data.v2.total": 100, "disks./mnt/data.v2.used": 40,
                               "disks./boot/firmware.total": 10, "memory.total": 8, "memory.available": 2})
    assert [d["mount"] for d in m.disks] == ["/boot/firmware", "/mnt/data.v2"]
    assert m.disks[1] == {"mount": "/mnt/data.v2", "size": 100, "used": 40, "free": 60}
    assert (m.ram_total, m.ram_used, m.ram_free) == (8, 6, 2)


def test_history_is_thinned_and_old_samples_go_to_history_only(db):
    node = Node(name="n1", token_hash="x")
    db.add(node)
    db.commit()
    now = int(time.time() * 1000)
    agents.handle_sample(db, node, {"ts": now, "values": {"cpu.percent": 10}})
    agents.handle_sample(db, node, {"ts": now + 1000, "values": {"cpu.percent": 20}})  # через 1 с — не в историю
    agents.handle_sample(db, node, {"ts": now - 600_000, "values": {"cpu.percent": 5}})  # из буфера, 10 мин назад
    assert node.values == {"cpu.percent": 20}
    assert len(db.scalars(select(NodeSample)).all()) == 2
    agents.handle_sample(db, node, {"ts": "bad", "values": {"cpu.percent": float("nan"), "x": True}})
    assert node.values == {"cpu.percent": 20}


@pytest.mark.parametrize("hello", [
    {"type": "hello", "node": "a b", "token": "t", "sensors": []},
    {"type": "hello", "node": "a", "token": "", "sensors": []},
    {"type": "hello", "node": "a", "token": "t", "sensors": [{"id": "x", "kind": "Bad Kind"}]},
    {"type": "sample"},
])
def test_parse_hello_rejects(hello):
    assert isinstance(agents.parse_hello(hello), str)


def test_local_collector_in_container_reports_no_disks(monkeypatch):
    monkeypatch.setattr(monitoring, "in_container", lambda: True)
    assert monitoring.collect_local().disks == []
