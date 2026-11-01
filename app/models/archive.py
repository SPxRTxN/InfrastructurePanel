"""Архив удалённого (сервер со всем связанным, узел) и журнал событий безопасности."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.utils import utcnow


class Archive(Base):
    """Снимок удалённого объекта с историей — gzip JSON; хранится `history.retention_days`, входит в бэкапы БД."""

    __tablename__ = "archives"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # server | node
    name: Mapped[str] = mapped_column(String(128))
    summary: Mapped[str] = mapped_column(String(500), default="")
    deleted_by: Mapped[str] = mapped_column(String(64), default="")
    deleted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    size: Mapped[int] = mapped_column(Integer, default=0)  # байт без сжатия
    data: Mapped[bytes] = mapped_column(LargeBinary)


class SecurityEvent(Base):
    """Журнал безопасности кроме попыток входа (они в login_attempts): выход, смена пароля, пользователи,
    токены и подключения агентов, удаления, скачивания архивов."""

    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(String(500), default="")
