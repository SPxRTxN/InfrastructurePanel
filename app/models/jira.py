from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.auth import User
from app.utils import utcnow

DEFAULT_COLUMNS = ["To Do", "In Progress", "Review", "Done"]
PRIORITIES = {"low": "Низкий", "medium": "Средний", "high": "Высокий", "critical": "Критический"}


class Board(Base):
    __tablename__ = "boards"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(10), unique=True)  # префикс ключа задач, например HL
    name: Mapped[str] = mapped_column(String(128))
    next_number: Mapped[int] = mapped_column(Integer, default=1)

    columns: Mapped[list["BoardColumn"]] = relationship(
        back_populates="board", cascade="all, delete-orphan", order_by="BoardColumn.position")
    issues: Mapped[list["Issue"]] = relationship(back_populates="board", cascade="all, delete-orphan")


class BoardColumn(Base):
    __tablename__ = "board_columns"

    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer, default=0)

    board: Mapped[Board] = relationship(back_populates="columns")
    issues: Mapped[list["Issue"]] = relationship(back_populates="column", order_by="Issue.position")


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("board_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    board_id: Mapped[int] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), index=True)
    column_id: Mapped[int] = mapped_column(ForeignKey("board_columns.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(10), default="medium")
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    due_date: Mapped[date | None] = mapped_column(Date)
    labels: Mapped[str] = mapped_column(String(500), default="")  # через запятую
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    board: Mapped[Board] = relationship(back_populates="issues")
    column: Mapped[BoardColumn] = relationship(back_populates="issues")
    assignee: Mapped[User | None] = relationship()

    @property
    def key(self) -> str:
        return f"{self.board.key}-{self.number}"

    @property
    def label_list(self) -> list[str]:
        return [x for x in self.labels.split(",") if x]

    @property
    def is_done(self) -> bool:
        # Готово = последняя колонка доски
        return bool(self.board.columns) and self.column_id == self.board.columns[-1].id
