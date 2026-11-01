from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SettingValue(Base):
    """Значение настройки из реестра. user_id = NULL — глобальная настройка."""

    __tablename__ = "setting_values"
    __table_args__ = (UniqueConstraint("key", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    value: Mapped[str] = mapped_column(Text)  # JSON
