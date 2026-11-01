"""Режим обслуживания панели на время обновления (одна строка, id = 1)."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.utils import utcnow


class Maintenance(Base):
    __tablename__ = "maintenance"

    id: Mapped[int] = mapped_column(primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    since: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # UTC
    eta: Mapped[datetime | None] = mapped_column(DateTime, default=None)  # UTC, «примерно до»
    by: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(String(255), default="")
    target_version: Mapped[str] = mapped_column(String(32), default="")
