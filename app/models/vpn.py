"""VPN: подписки (ссылка и серверы — зашифрованы) и выбор сервера для узла."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.nodes import Node
from app.utils import utcnow


class VpnSubscription(Base):
    __tablename__ = "vpn_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    url_enc: Mapped[str] = mapped_column(Text)  # Fernet: в ссылке подписки — личный токен
    insecure: Mapped[bool] = mapped_column(Boolean, default=False)  # не проверять TLS (https://<IP> с самоподписанным)
    servers_enc: Mapped[str] = mapped_column(Text, default="")  # Fernet(JSON) — с uuid/паролями
    count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    error: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VpnAssignment(Base):
    """Какой сервер подписки включить на узле. Узел применяет сам (агент с разрешением в своём конфиге)."""

    __tablename__ = "vpn_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), unique=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("vpn_subscriptions.id", ondelete="CASCADE"))
    server_key: Mapped[str] = mapped_column(String(16))
    server_name: Mapped[str] = mapped_column(String(128), default="")
    mode: Mapped[str] = mapped_column(String(16), default="all")  # all — весь трафик узла; off — выключено
    # pending — ждёт агента; applied — применено; failed — откатился; unsupported — агент без VPN
    status: Mapped[str] = mapped_column(String(16), default="pending")
    detail: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    node: Mapped[Node] = relationship()
    subscription: Mapped[VpnSubscription] = relationship()
