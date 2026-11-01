"""Журнал бэкапов и проверок восстановления."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.utils import utcnow


class BackupLog(Base):
    __tablename__ = "backup_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)  # UTC
    kind: Mapped[str] = mapped_column(String(8), default="backup")  # backup | verify
    trigger: Mapped[str] = mapped_column(String(8), default="auto")  # auto | manual
    file: Mapped[str] = mapped_column(String(255), default="")  # только имя файла, без пути
    size: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(String(500), default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)  # проверка восстановления выполнялась
    verify_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    verify_detail: Mapped[str] = mapped_column(String(500), default="")
