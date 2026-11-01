"""Оповещения: правила, состояние по каждому объекту и журнал срабатываний."""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Table, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.auth import Group
from app.utils import utcnow


# Кому уходит колокольчик правила. Пусто — всем активным с servers.view (как было до групп).
alert_rule_groups = Table(
    "alert_rule_groups",
    Base.metadata,
    Column("rule_id", ForeignKey("alert_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


class AlertRule(Base):
    """Правило: вид проверки + порог + сколько минут держится, прежде чем сработать."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    # node_offline | server_offline | service_down | disk | temperature | sensor
    kind: Mapped[str] = mapped_column(String(24))
    threshold: Mapped[float | None] = mapped_column(Float, default=None)
    # Для kind=sensor: id датчика агента (например systemd.nginx.service.active) и сравнение
    sensor: Mapped[str] = mapped_column(String(200), default="")
    op: Mapped[str] = mapped_column(String(2), default=">=")
    # Пусто — все объекты; иначе имя узла или сервера (точное совпадение)
    target: Mapped[str] = mapped_column(String(128), default="")
    for_minutes: Mapped[int] = mapped_column(Integer, default=5)
    repeat_minutes: Mapped[int] = mapped_column(Integer, default=0)  # 0 — не повторять, пока горит
    severity: Mapped[str] = mapped_column(String(16), default="warning")  # warning | critical
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_bell: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_webhook: Mapped[bool] = mapped_column(Boolean, default=False)
    groups: Mapped[list[Group]] = relationship(secondary=alert_rule_groups)


class AlertState(Base):
    """Состояние правила для одного объекта: «узел pi», «диск / сервера Mini PC» и т.п."""

    __tablename__ = "alert_states"
    __table_args__ = (UniqueConstraint("rule_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(300), default="")
    link: Mapped[str] = mapped_column(String(255), default="")
    value: Mapped[str] = mapped_column(String(64), default="")
    pending_since: Mapped[datetime | None] = mapped_column(DateTime, default=None)  # условие выполнено с
    firing: Mapped[bool] = mapped_column(Boolean, default=False)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_sent: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # объект ещё существует


class AlertLog(Base):
    """Журнал: сработало / прошло (хранится history.retention_days)."""

    __tablename__ = "alert_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    rule_name: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))  # firing | resolved
    title: Mapped[str] = mapped_column(String(300))
    value: Mapped[str] = mapped_column(String(64), default="")
    link: Mapped[str] = mapped_column(String(255), default="")
    delivered: Mapped[str] = mapped_column(String(100), default="")  # bell, telegram, webhook / тишина / ошибки
