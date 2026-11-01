"""Оповещения: правила, ожидание → горит → прошло, тишина, каналы, права."""
import asyncio
import json
from datetime import timedelta

from sqlalchemy import func, select

from app.models import AlertLog, AlertRule, AlertState, Node, Notification, Server, ServerMetric, SettingValue
from app.services import alerts
from app.utils import utcnow


def _node(db, name="pi", seen_ago=0, connected=True, values=None, sensors=None):
    n = Node(name=name, token_hash="x" * 64, connected=connected, last_seen=utcnow() - timedelta(minutes=seen_ago),
             values_json=json.dumps(values or {}), sensors_json=json.dumps(sensors or []))
    db.add(n)
    db.commit()
    return n


def _run(db, now):
    asyncio.run(alerts.deliver(db, alerts.evaluate(db, now)))


def test_node_offline_pending_firing_resolved(db):
    db.add(AlertRule(name="Узел не на связи", kind="node_offline", for_minutes=3, severity="critical"))
    n = _node(db, seen_ago=10, connected=False)
    now = utcnow()
    _run(db, now)
    st = db.scalar(select(AlertState))
    assert st.pending_since and not st.firing  # ещё ждём 3 минуты
    _run(db, now + timedelta(minutes=4))
    db.expire_all()
    assert db.scalar(select(AlertState)).firing
    note = db.scalar(select(Notification).where(Notification.title.like("Тревога%")))
    assert "узел pi" in note.title and note.link == f"/nodes/{n.id}"
    # Узел вернулся — «Норма»
    n = db.get(Node, n.id)
    n.connected, n.last_seen = True, utcnow()
    db.commit()
    _run(db, utcnow())
    assert db.scalar(select(Notification).where(Notification.title.like("Норма%")))
    assert [e.state for e in db.scalars(select(AlertLog).order_by(AlertLog.id))] == ["firing", "resolved"]


def test_disk_and_temperature_and_sensor(db):
    s = Server(name="Mini", host="10.0.0.1", collector="agent", online=True)
    db.add(s)
    db.flush()
    db.add(ServerMetric(server_id=s.id, online=True, disks=json.dumps([{"mount": "/", "size": 100, "used": 95, "free": 5},
                                                                      {"mount": "/boot", "size": 100, "used": 10, "free": 90}])))
    db.add_all([AlertRule(name="Диск", kind="disk", threshold=90, for_minutes=0),
                AlertRule(name="Жарко", kind="temperature", threshold=75, for_minutes=0),
                AlertRule(name="nginx упал", kind="sensor", sensor="systemd.nginx.service.active", op="==",
                          threshold=0, for_minutes=0)])
    _node(db, values={"thermal.cpu.temp": 81.5, "systemd.nginx.service.active": 0},
          sensors=[{"id": "thermal.cpu.temp", "kind": "temperature", "label": "CPU", "unit": "°C"},
                   {"id": "systemd.nginx.service.active", "kind": "state", "label": "nginx.service", "unit": ""}])
    _run(db, utcnow())
    titles = {(st.title, st.value) for st, _ in alerts.firing(db)}
    assert ("диск / — сервер Mini", "95 %") in titles
    assert ("CPU на pi", "81.5 °C") in titles
    assert ("nginx.service на pi", "0") in titles
    assert not any("/boot" in t for t, _ in titles)


def test_silence_skips_delivery(db):
    from app.services.settings_registry import set_setting

    set_setting(db, "alerts.silence_until", (utcnow() + timedelta(hours=1)).isoformat())
    db.add(AlertRule(name="Узел", kind="node_offline", for_minutes=0))
    _node(db, seen_ago=10, connected=False)
    _run(db, utcnow())
    assert db.scalar(select(func.count()).select_from(Notification)) == 0
    assert db.scalar(select(AlertLog)).delivered == "тишина"


def test_object_removed_resolves(db):
    db.add(AlertRule(name="Узел", kind="node_offline", for_minutes=0))
    n = _node(db, seen_ago=10, connected=False)
    _run(db, utcnow())
    db.delete(db.get(Node, n.id))
    db.commit()
    _run(db, utcnow())
    assert db.scalar(select(func.count()).select_from(AlertState)) == 0
    assert db.scalar(select(AlertLog).order_by(AlertLog.id.desc())).value == "объекта больше нет"


def test_channels_telegram_webhook(db, monkeypatch):
    from app.services.security import encrypt
    from app.services.settings_registry import set_setting

    sent = []
    monkeypatch.setattr(alerts, "send_telegram", lambda token, chat, text: sent.append(("tg", token, chat, text)) or "")
    monkeypatch.setattr(alerts, "send_webhook", lambda url, kind, title, body, sev, state: sent.append(("wh", url, title)) or "HTTP 500")
    set_setting(db, "alerts.telegram_token", encrypt("123:ABC"))
    set_setting(db, "alerts.telegram_chat", "42")
    set_setting(db, "alerts.webhook_url", "https://ntfy.example/topic")
    db.add(AlertRule(name="Узел", kind="node_offline", for_minutes=0, notify_telegram=True, notify_webhook=True))
    _node(db, seen_ago=10, connected=False)
    _run(db, utcnow())
    assert sent[0][:3] == ("tg", "123:ABC", "42") and "Тревога: Узел — узел pi" in sent[0][3]
    assert sent[1][0] == "wh"
    assert db.scalar(select(AlertLog)).delivered == "колокольчик, telegram, webhook: HTTP 500"


def test_page_defaults_and_permissions(admin, user, viewer, db):
    assert viewer.get("/alerts").status_code == 200
    assert viewer.post("/alerts/defaults").status_code == 403
    assert admin.post("/alerts/defaults", follow_redirects=False).status_code == 303
    assert db.scalar(select(func.count()).select_from(AlertRule)) == 5
    assert admin.post("/alerts/defaults").status_code == 400  # второй раз — нет
    r = admin.post("/alerts/rules", data={"kind": "disk", "threshold": "", "for_minutes": "5"})
    assert r.status_code == 400  # без порога
    admin.post("/alerts/rules", data={"kind": "sensor", "sensor": "cpu.percent", "op": ">", "threshold": "95",
                                      "for_minutes": "10", "enabled": "on", "notify_bell": "on"})
    rule = db.scalar(select(AlertRule).where(AlertRule.kind == "sensor"))
    assert rule.threshold == 95 and rule.op == ">" and rule.name == "Датчик агента"
    # Каналы — только settings.manage; токен хранится зашифрованным и не показывается
    assert user.post("/alerts/channels", data={"telegram_token": "1:X"}).status_code == 403
    admin.post("/alerts/channels", data={"telegram_token": "123:SECRET", "telegram_chat": "42"})
    raw = db.scalar(select(SettingValue).where(SettingValue.key == "alerts.telegram_token")).value
    assert "SECRET" not in raw
    page = admin.get("/alerts").text
    assert "SECRET" not in page and "задан — оставьте пустым" in page
    assert "Тревог сейчас" in admin.get("/").text


def test_silence_buttons(admin, db):
    admin.post("/alerts/silence", data={"minutes": "60"})
    assert alerts.silenced(db)
    assert "Снять тишину" in admin.get("/alerts").text
    admin.post("/alerts/silence", data={"minutes": "0"})
    assert alerts.silenced(db) is None


def test_test_send(admin, db, monkeypatch):
    monkeypatch.setattr(alerts, "send_webhook", lambda *a: "")
    from app.services.settings_registry import set_setting

    set_setting(db, "alerts.webhook_url", "https://ntfy.example/t")
    r = admin.post("/alerts/test")
    assert "колокольчик: ok" in r.text and "webhook: ok" in r.text
