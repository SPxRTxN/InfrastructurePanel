"""Каталог версий агента (архивы лежат в <каталог данных>/agent-releases/) и журнал обновлений узлов."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.nodes import Node
from app.utils import utcnow


class AgentRelease(Base):
    __tablename__ = "agent_releases"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(32), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(String(500), default="")
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NodeUpdate(Base):
    """Одно обновление узла. Статусы: sent (ушло агенту) → switching (агент проверил и переключил версию, ждём его
    возвращения) → done (вернулся с новой версией); failed — отказ агента; unconfirmed — не вернулся за 5 минут;
    rolled_back — вернулся со старой версией (сторож агента откатил)."""

    __tablename__ = "node_updates"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    from_version: Mapped[str] = mapped_column(String(32), default="")
    to_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="sent")
    detail: Mapped[str] = mapped_column(String(500), default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    node: Mapped[Node] = relationship()
