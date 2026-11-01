from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.auth import User
from app.utils import utcnow

REPEATS = {"none": "Не повторять", "daily": "Каждый день", "weekly": "Каждую неделю", "monthly": "Каждый месяц"}


class Event(Base):
    """Событие календаря. start/end — локальное время домашней лаборатории (без часового пояса)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    start: Mapped[datetime] = mapped_column(DateTime, index=True)
    end: Mapped[datetime | None] = mapped_column(DateTime)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    repeat: Mapped[str] = mapped_column(String(10), default="none")
    remind_minutes: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    author: Mapped[User | None] = relationship()


class ReminderLog(Base):
    """Отметка «напоминание о вхождении отправлено», чтобы не слать повторно."""

    __tablename__ = "reminder_log"
    __table_args__ = (UniqueConstraint("event_id", "occurrence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    occurrence: Mapped[datetime] = mapped_column(DateTime)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
