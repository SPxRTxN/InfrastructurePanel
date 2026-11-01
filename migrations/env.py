from alembic import context

import app.models  # noqa: F401 — регистрирует все таблицы в Base.metadata
from app.config import config as app_config
from app.db import Base, make_engine

target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url") or app_config.database_url
    # foreign_keys=OFF: batch-режим пересоздаёт таблицу (CREATE → копия → DROP → RENAME), и DROP при
    # включённых внешних ключах каскадно удалил бы строки связанных таблиц (членство в группах, сессии…)
    engine = make_engine(url, foreign_keys=False)
    with engine.connect() as connection:
        # render_as_batch: SQLite не умеет ALTER COLUMN, Alembic пересоздаёт таблицу
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
            # С выключенными ключами нарушения не ловятся сами — проверяем явно и откатываем миграцию
            broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(f"После миграции нарушены внешние ключи: {broken[:5]}")
    engine.dispose()


run_migrations_online()
