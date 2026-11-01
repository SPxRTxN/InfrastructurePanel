"""Оповещения: получатели колокольчика по группам и событие «агент переподключился»."""
import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.models import AlertRule, Group, Node, NodeEvent, Notification, User
from app.services import agents, alerts
from app.utils import utcnow


def _group(db, name):
    return db.scalar(select(Group).where(Group.name == name))


def _users_notified(db):
    return {db.get(User, n.user_id).username for n in db.scalars(select(Notification))}


def _fire_node_offline(db, rule):
    db.add(rule)
    db.add(Node(name="pi", token_hash="x" * 64, connected=False, last_seen=utcnow() - timedelta(minutes=30)))
    db.commit()
    asyncio.run(alerts.deliver(db, alerts.evaluate(db, utcnow())))


def test_no_groups_bell_goes_to_everyone_with_view(db):
    _fire_node_offline(db, AlertRule(name="Узел", kind="node_offline", for_minutes=0))
    assert _users_notified(db) == {"admin", "user", "viewer"}


def test_group_limits_bell(db):
    rule = AlertRule(name="Узел", kind="node_offline", for_minutes=0, groups=[_group(db, "Наблюдатели")])
    _fire_node_offline(db, rule)
    assert _users_notified(db) == {"viewer"}


def test_group_member_without_servers_view_gets_nothing(db):
    g = Group(name="Без доступа")
    db.add(g)
    db.add(User(username="nobody", password_hash="x", groups=[g]))
    db.commit()
    _fire_node_offline(db, AlertRule(name="Узел", kind="node_offline", for_minutes=0, groups=[g]))
    assert _users_notified(db) == set()


def test_form_saves_groups_and_validates(admin, user, db):
    gid = _group(db, "Наблюдатели").id
    base = {"kind": "node_offline", "for_minutes": "3", "enabled": "on", "notify_bell": "on"}
    assert admin.post("/alerts/rules", data={**base, "group_ids": [str(gid)]}, follow_redirects=False).status_code == 303
    rule = db.scalar(select(AlertRule))
    assert [g.name for g in rule.groups] == ["Наблюдатели"]
    assert "колокольчик: Наблюдатели" in admin.get("/alerts").text
    assert admin.post("/alerts/rules", data={**base, "group_ids": ["99999"]}).status_code == 400
    assert admin.post("/alerts/rules", data={**base, "group_ids": ["abc"]}).status_code == 400
    # Правка без групп возвращает «всем»
    admin.post(f"/alerts/rules/{rule.id}", data=base)
    db.expire_all()
    assert db.get(AlertRule, rule.id).groups == []


def test_edit_permissions_unchanged(user, viewer, db):
    gid = str(_group(db, "Наблюдатели").id)
    data = {"kind": "node_offline", "for_minutes": "3", "group_ids": [gid]}
    assert viewer.post("/alerts/rules", data=data).status_code == 403
    assert user.post("/alerts/rules", data=data, follow_redirects=False).status_code == 303  # servers.edit


def test_hello_records_restart_and_rule_fires_once(db):
    token = "t" * 40
    node = Node(name="pi", token_hash=agents.hash_token(token), version="0.1",
                last_seen=utcnow() - timedelta(minutes=20))
    db.add(node)
    db.add(AlertRule(name="Агент вернулся", kind="agent_restarted", for_minutes=0, threshold=5, severity="warning"))
    db.commit()
    hello = {"node": "pi", "token": token, "version": "0.2", "info": {}, "sensors": [], "actions": [], "ip": []}
    assert agents.handle_hello(db, hello, "10.0.0.2") is node
    ev = db.scalar(select(NodeEvent))
    assert ev.text.startswith(agents.RESTART_MARK) and "обрыв 20 мин" in ev.text and "0.1 → 0.2" in ev.text
    asyncio.run(alerts.deliver(db, alerts.evaluate(db, utcnow())))
    assert db.scalar(select(Notification).where(Notification.title.like("Тревога: Агент вернулся%")))
    # Событие старше окна исчезает без «Нормы»
    ev.ts = utcnow() - timedelta(hours=2)
    db.commit()
    asyncio.run(alerts.deliver(db, alerts.evaluate(db, utcnow())))
    assert not db.scalar(select(Notification).where(Notification.title.like("Норма%")))


def test_short_gap_same_version_no_event_and_below_threshold(db):
    token = "t" * 40
    node = Node(name="pi", token_hash=agents.hash_token(token), version="0.1", last_seen=utcnow())
    db.add(node)
    db.commit()
    hello = {"node": "pi", "token": token, "version": "0.1", "info": {}, "sensors": [], "actions": [], "ip": []}
    agents.handle_hello(db, hello, "10.0.0.2")
    assert db.scalar(select(NodeEvent)) is None
    # Обрыв 3 мин (записан), порог правила 5 — не срабатывает
    db.add(NodeEvent(node_id=node.id, text=agents.RESTART_MARK + "обрыв 3 мин"))
    db.add(AlertRule(name="Агент вернулся", kind="agent_restarted", for_minutes=0, threshold=5))
    db.commit()
    asyncio.run(alerts.deliver(db, alerts.evaluate(db, utcnow())))
    assert not alerts.firing(db)
