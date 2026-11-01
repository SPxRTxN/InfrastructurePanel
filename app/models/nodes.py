"""Узлы с агентом homelab-agent: самоописание, последние значения, история, действия и события."""
import json
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.servers import Server
from app.utils import utcnow


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # Хеш токена узла; сам токен показывается один раз при выдаче
    token_hash: Mapped[str] = mapped_column(String(64))
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    version: Mapped[str] = mapped_column(String(32), default="")
    remote_ip: Mapped[str] = mapped_column(String(64), default="")
    # JSON из hello: info {os, kernel, ...}, sensors [{id, kind, label, unit}], actions [{name, label}], ip [...]
    info_json: Mapped[str] = mapped_column(Text, default="{}")
    sensors_json: Mapped[str] = mapped_column(Text, default="[]")
    actions_json: Mapped[str] = mapped_column(Text, default="[]")
    ips_json: Mapped[str] = mapped_column(Text, default="[]")
    # Последняя выборка {id: значение} и её время
    values_json: Mapped[str] = mapped_column(Text, default="{}")
    values_ts: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    server: Mapped[Server | None] = relationship()
    samples: Mapped[list["NodeSample"]] = relationship(cascade="all, delete-orphan", passive_deletes=True)

    @property
    def info(self) -> dict:
        return json.loads(self.info_json)

    @property
    def sensors(self) -> list[dict]:
        return json.loads(self.sensors_json)

    @property
    def actions(self) -> list[dict]:
        return json.loads(self.actions_json)

    @property
    def ips(self) -> list[str]:
        return json.loads(self.ips_json)

    @property
    def values(self) -> dict[str, float]:
        return json.loads(self.values_json)


class NodeSample(Base):
    """Прореженная история значений (не чаще раза в `agents.history_every` секунд), хранится 24 часа."""

    __tablename__ = "node_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    values_json: Mapped[str] = mapped_column(Text)


class NodeAction(Base):
    """Запрос действия восстановления из allowlist узла и его результат."""

    __tablename__ = "node_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent | ok | failed | lost
    code: Mapped[int | None] = mapped_column(Integer, default=None)
    output: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(String(200), default="")


class NodeEvent(Base):
    """События, о которых сообщил агент (например, автовосстановление сервиса)."""

    __tablename__ = "node_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")  # info | warning | error
    text: Mapped[str] = mapped_column(String(500))


class NodeTombstone(Base):
    """«Надгробие» удалённого узла: агент не был на связи при удалении. Если он вернётся со старым токеном,
    панель ответит ему командой uninstall вместо отказа. Хранится имя и хеш токена (не сам токен), 30 дней."""

    __tablename__ = "node_tombstones"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
