from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.utils import utcnow


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    host: Mapped[str] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    collector: Mapped[str] = mapped_column(String(8), default="local")  # local | ssh
    ssh_user: Mapped[str] = mapped_column(String(64), default="")
    # Путь к ключу зашифрован Fernet и никогда не отдаётся в UI/API
    ssh_key_enc: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    online: Mapped[bool | None] = mapped_column(Boolean, default=None)
    ssh_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_error: Mapped[str] = mapped_column(String(500), default="")

    services: Mapped[list["Service"]] = relationship(back_populates="server", cascade="all, delete-orphan",
                                                     order_by="Service.name")
    metrics: Mapped[list["ServerMetric"]] = relationship(cascade="all, delete-orphan", passive_deletes=True)

    @property
    def has_key(self) -> bool:
        return bool(self.ssh_key_enc)


class ServerMetric(Base):
    """Снимок метрик за одну проверку; хранится 24 часа."""

    __tablename__ = "server_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    online: Mapped[bool] = mapped_column(Boolean)
    ssh_ok: Mapped[bool | None] = mapped_column(Boolean)
    ram_total: Mapped[int | None] = mapped_column(BigInteger)
    ram_used: Mapped[int | None] = mapped_column(BigInteger)
    ram_free: Mapped[int | None] = mapped_column(BigInteger)
    disks: Mapped[str] = mapped_column(Text, default="[]")  # JSON: [{mount, size, used, free}]


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    check_type: Mapped[str] = mapped_column(String(8), default="tcp")  # tcp | http
    target: Mapped[str] = mapped_column(String(500))  # host:port или URL
    ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_error: Mapped[str] = mapped_column(String(500), default="")

    server: Mapped[Server] = relationship(back_populates="services")
