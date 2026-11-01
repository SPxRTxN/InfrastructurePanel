"""Запись сеанса SSH-консоли (только вывод, asciicast v2 в gzip). Лежит в БД: попадает под SQLCipher и бэкапы."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.utils import utcnow


class ConsoleRecording(Base):
    __tablename__ = "console_recordings"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), index=True)
    server_name: Mapped[str] = mapped_column(String(120), default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    username: Mapped[str] = mapped_column(String(64), default="")
    target: Mapped[str] = mapped_column(String(255), default="")  # user@host:port, без паролей
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # UTC
    ended_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    bytes: Mapped[int] = mapped_column(Integer, default=0)  # записанный вывод до сжатия
    data: Mapped[bytes] = mapped_column(LargeBinary, default=b"")  # gzip(asciicast v2)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(120), default="")
