"""Сквозная совместимость: сообщения настоящего homelab-agent (handoff/homelab-agent) принимает панель.

Тесты агента проверяют его на фейковой панели, тесты панели — на фейковом агенте; здесь — настоящие
коллекторы агента (чтение /proc и /sys этой машины) и настоящий приёмник панели через TestClient.
"""
import json
import re
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Node
from app.services import agents

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "handoff" / "homelab-agent"))
protocol = pytest.importorskip("homelab_agent.protocol")
from homelab_agent.collectors.base import build_collectors  # noqa: E402


def test_real_agent_messages_accepted(admin, db):
    r = admin.post("/nodes", data={"name": "contract-node", "server_id": ""})
    token = re.search(r'readonly value="([^"]+)"', r.text).group(1)

    collectors = build_collectors(None, 10)
    sensors = [s.as_dict() for c in collectors for s in c.sensors()]
    values = {}
    for c in collectors:
        values.update(c.read())
    hello = protocol.hello("contract-node", token, sensors,
                           [{"name": "restart_x", "label": "Перезапустить x"}], {"ip": ["127.0.0.1"], "info": {}})

    with admin.websocket_connect("/agent/ws") as ws:
        ws.send_text(protocol.encode(hello))
        assert json.loads(ws.receive_text())["type"] == "welcome"
        ws.send_text(protocol.encode(protocol.sample(int(time.time() * 1000), values)))
        ws.send_text(protocol.encode(protocol.event(int(time.time() * 1000), "info", "проверка")))
        for _ in range(50):
            db.expire_all()
            node = db.scalar(select(Node).where(Node.name == "contract-node"))
            if node.values_ts:
                break
            time.sleep(0.05)

    assert len(node.sensors) == len(sensors)  # ни один датчик агента не отвергнут
    assert node.actions == [{"name": "restart_x", "label": "Перезапустить x"}]
    metrics = agents.server_metrics(node.values, node.sensors)
    assert metrics.ram_total and metrics.ram_used is not None
    # Разделы — ровно те, что агент прислал как disks.<mount>.total
    mounts = {k[len("disks."):-len(".total")] for k in values if k.startswith("disks.") and k.endswith(".total")}
    assert {d["mount"] for d in metrics.disks} == mounts
