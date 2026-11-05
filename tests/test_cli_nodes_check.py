"""cli nodes-check: состояние узлов и последние входы агентов, без токенов."""
from app import cli
from app.models import Node, SecurityEvent
from app.utils import utcnow


def test_nodes_check_reports_state_and_auth(db, capsys):
    n = Node(name="example", token_hash="h" * 64)
    db.add(n)
    db.add(SecurityEvent(kind="agent_auth", ok=False, username="example", ip="192.0.2.7", detail="bad token", ts=utcnow()))
    db.add(SecurityEvent(kind="agent_auth", ok=False, username="other", ip="192.0.2.8", detail="bad token", ts=utcnow()))
    db.commit()
    assert cli.nodes_check() == 0
    out = capsys.readouterr().out
    assert "example: НЕ на связи" in out and "ОТКАЗ — bad token" in out
    assert "Чужое имя узла «other»" in out and "h" * 64 not in out
