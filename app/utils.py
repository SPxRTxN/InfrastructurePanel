from datetime import UTC, datetime


def utcnow() -> datetime:
    """Наивное UTC-время: SQLite не хранит часовой пояс, поэтому везде храним UTC без tzinfo."""
    return datetime.now(UTC).replace(tzinfo=None)
