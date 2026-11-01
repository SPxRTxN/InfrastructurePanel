"""Программный запуск миграций Alembic (при старте приложения, в CLI и тестах)."""
from alembic import command
from alembic.config import Config as AlembicConfig

from app.config import BASE_DIR, config


def alembic_config(url: str | None = None) -> AlembicConfig:
    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url or config.database_url)
    return cfg


def upgrade(url: str | None = None) -> None:
    command.upgrade(alembic_config(url), "head")
