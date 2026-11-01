from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    # Устарело с 0025: ключи хостов — в ssh_host_keys (по адресу и порту); столбец оставлен, чтобы не пересоздавать servers
    host_key: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Тип устройства: server (есть BMC) | pc (компьютер) | other. Влияет только на блоки в настройках узла
    kind: Mapped[str] = mapped_column(String(8), default="server", server_default="server")

    online: Mapped[bool | None] = mapped_column(Boolean, default=None)
    ssh_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_error: Mapped[str] = mapped_column(String(500), default="")

    services: Mapped[list["Service"]] = relationship(back_populates="server", cascade="all, delete-orphan",
                                                     order_by="Service.name")
    credentials: Mapped[list["ServerCredential"]] = relationship(back_populates="server", cascade="all, delete-orphan",
                                                                 order_by="ServerCredential.id")
    metrics: Mapped[list["ServerMetric"]] = relationship(cascade="all, delete-orphan", passive_deletes=True)
    host_keys: Mapped[list["SshHostKey"]] = relationship(back_populates="server", cascade="all, delete-orphan",
                                                          order_by="SshHostKey.id")

    @property
    def has_key(self) -> bool:
        return bool(self.ssh_key_enc)

    def known_key(self, host: str, port: int) -> "SshHostKey | None":
        return next((k for k in self.host_keys if k.host == host and k.port == port), None)

    def allowed_hosts(self) -> set[str]:
        """Адреса, куда консоль этого сервера может подключаться: сам сервер и его сохранённые входы (BMC)."""
        return {self.host} | {c.host for c in self.credentials if c.host}


class ServerCredential(Base):
    """Сохранённый вход для SSH-консоли и передачи файлов: ОС сервера или его BMC (у BMC свой адрес).

    Пароль зашифрован Fernet (SECRET_KEY) и никогда не отдаётся в UI/API — только «сохранён / нет».
    """

    __tablename__ = "server_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8), default="os")  # os | bmc
    label: Mapped[str] = mapped_column(String(64), default="")
    host: Mapped[str] = mapped_column(String(255), default="")  # пусто — адрес сервера
    port: Mapped[int] = mapped_column(Integer, default=22)  # как у ssh: не указан — 22
    username: Mapped[str] = mapped_column(String(64))
    password_enc: Mapped[str] = mapped_column(Text, default="")
    host_key: Mapped[str] = mapped_column(Text, default="")  # устарело с 0025 — см. ssh_host_keys
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    server: Mapped[Server] = relationship(back_populates="credentials")

    @property
    def has_password(self) -> bool:
        return bool(self.password_enc)


class SshHostKey(Base):
    """Ключ хоста, запомненный при первом подключении (accept-new) — отдельно для каждой пары адрес+порт,
    как строки known_hosts у OpenSSH: на другом порту может работать другой sshd (контейнер, BMC)."""

    __tablename__ = "ssh_host_keys"
    __table_args__ = (UniqueConstraint("server_id", "host", "port"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    key_line: Mapped[str] = mapped_column(Text)  # строка OpenSSH: «ssh-ed25519 AAAA…»
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    server: Mapped[Server] = relationship(back_populates="host_keys")


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
