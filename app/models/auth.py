from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Table, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.utils import utcnow

user_groups = Table(
    "user_groups",
    Base.metadata,
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Администратор по умолчанию admin/admin: пока не сменит логин и пароль, доступна только /setup
    must_change_credentials: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    groups: Mapped[list["Group"]] = relationship(secondary=user_groups, back_populates="users")

    @property
    def name(self) -> str:
        return self.display_name or self.username

    @property
    def permissions(self) -> set[str]:
        return {p.perm for g in self.groups for p in g.permissions}

    # Выключенные в настройках разделы (префиксы прав); заполняет current_user на время запроса
    disabled_modules: frozenset[str] = frozenset()

    def can(self, perm: str) -> bool:
        return perm in self.permissions and perm.split(".", 1)[0] not in self.disabled_modules


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    users: Mapped[list[User]] = relationship(secondary=user_groups, back_populates="groups")
    permissions: Mapped[list["GroupPermission"]] = relationship(
        cascade="all, delete-orphan", back_populates="group"
    )


class GroupPermission(Base):
    __tablename__ = "group_permissions"
    __table_args__ = (UniqueConstraint("group_id", "perm"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    perm: Mapped[str] = mapped_column(String(64))

    group: Mapped[Group] = relationship(back_populates="permissions")


class UserSession(Base):
    """Сессия входа. В БД хранится только sha256 от токена из cookie."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # Последнее действие человека (фоновые опросы не считаются) — для тайм-аута бездействия
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    user: Mapped[User] = relationship()


class LoginAttempt(Base):
    """Журнал входов; неудачные записи используются для блокировки перебора."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    ip: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    success: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
