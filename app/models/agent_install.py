"""Установка агента из панели: одноразовый код (в БД только SHA-256), ход установки и хвост журнала."""
import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.utils import utcnow


class AgentInstall(Base):
    """status: waiting (код выдан, ждём) → running → ok | failed. method: command | ssh."""
    __tablename__ = "agent_installs"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    vpn: Mapped[bool] = mapped_column(Boolean, default=False)
    own_python: Mapped[bool] = mapped_column(Boolean, default=False)  # --own-python: свой Python агента
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    token_issued_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    method: Mapped[str] = mapped_column(String(16), default="command")
    status: Mapped[str] = mapped_column(String(16), default="waiting")
    steps_json: Mapped[str] = mapped_column(Text, default="[]")
    log: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    @property
    def steps(self) -> list[dict]:
        try:
            return json.loads(self.steps_json)
        except ValueError:
            return []
