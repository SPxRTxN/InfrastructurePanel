from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.auth import User
from app.models.jira import Issue
from app.utils import utcnow

# Связь задача ↔ статья: одна таблица, поэтому связь видна с обеих сторон
issue_pages = Table(
    "issue_pages",
    Base.metadata,
    Column("issue_id", ForeignKey("issues.id", ondelete="CASCADE"), primary_key=True),
    Column("page_id", ForeignKey("pages.id", ondelete="CASCADE"), primary_key=True),
)


class Space(Base):
    __tablename__ = "spaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(1000), default="")

    pages: Mapped[list["Page"]] = relationship(back_populates="space", cascade="all, delete-orphan",
                                               order_by="Page.title")


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    space_id: Mapped[int] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("pages.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    space: Mapped[Space] = relationship(back_populates="pages")
    parent: Mapped["Page | None"] = relationship(remote_side="Page.id")
    author: Mapped[User | None] = relationship()
    versions: Mapped[list["PageVersion"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="PageVersion.version.desc()")
    issues: Mapped[list[Issue]] = relationship(secondary=issue_pages, backref="pages", order_by=Issue.id)


class PageVersion(Base):
    """Снимок статьи на момент сохранения (включая текущую версию)."""

    __tablename__ = "page_versions"
    __table_args__ = (UniqueConstraint("page_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    author: Mapped[User | None] = relationship()
