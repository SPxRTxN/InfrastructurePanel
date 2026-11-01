"""Подключение к SQLite через SQLAlchemy 2.

С DB_ENCRYPTION_KEY база целиком шифруется SQLCipher (AES-256): файл БД, -wal и бэкапы без ключа
нечитаемы (аудит S-01). Без ключа — обычный SQLite, как раньше.
"""
import os
import re
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import config

SQLITE_HEADER = b"SQLite format 3\x00"


class Base(DeclarativeBase):
    pass


def sqlite_path(url: str) -> Path | None:
    return Path(url.removeprefix("sqlite:///")) if url.startswith("sqlite:///") else None


def _key_pragma(key: str) -> str:
    # Сырой 256-битный ключ (x'…'), без PBKDF2: ключ и так случайный, а KDF замедлял бы каждое подключение
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        raise ValueError("DB_ENCRYPTION_KEY: нужно 64 hex-символа. Сгенерировать: "
                         "python3 -c \"import secrets; print(secrets.token_hex(32))\"")
    return f"PRAGMA key = \"x'{key}'\""


def _dbapi(key: str):
    if not key:
        return sqlite3
    import sqlcipher3  # только при включённом шифровании

    return sqlcipher3.dbapi2


def _apply_pragmas(dbapi_conn, key: str, foreign_keys: bool = True) -> None:
    if key:
        dbapi_conn.execute(_key_pragma(key))  # должен идти первым, до любого чтения
    # SQLite по умолчанию не проверяет внешние ключи и не каскадирует удаление.
    # Миграции — с OFF: batch-режим Alembic пересоздаёт таблицу через DROP, а DROP при ON
    # каскадно удаляет связанные строки (так 0007 стёрла членство в группах, 2026-10-31)
    dbapi_conn.execute("PRAGMA foreign_keys=ON" if foreign_keys else "PRAGMA foreign_keys=OFF")
    # WAL: чтение не блокируется записью планировщика; busy_timeout вместо мгновенного 'database is locked'
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA busy_timeout=5000")
    # Удалённое затирается нулями, а не остаётся в свободных страницах файла (аудит S-07).
    # Явно: значение по умолчанию зависит от того, как собран SQLite в системе/образе
    dbapi_conn.execute("PRAGMA secure_delete=ON")


def connect_raw(path: Path, key: str | None = None):
    """DB-API подключение в обход SQLAlchemy (бэкап, аудит) — с тем же ключом и PRAGMA."""
    key = config.db_encryption_key if key is None else key
    conn = _dbapi(key).connect(path)
    _apply_pragmas(conn, key)
    return conn


def is_encrypted_file(path: Path) -> bool:
    """Непустой файл без заголовка SQLite — зашифрованный SQLCipher."""
    with open(path, "rb") as f:
        head = f.read(16)
    return bool(head) and head != SQLITE_HEADER


def check_db_encryption(url: str | None = None, key: str | None = None) -> None:
    """Понятная ошибка вместо 'file is not a database', если ключ и файл не совпадают по режиму."""
    path = sqlite_path(url or config.database_url)
    key = config.db_encryption_key if key is None else key
    if not path or not path.exists() or path.stat().st_size == 0:
        return
    encrypted = is_encrypted_file(path)
    if key and not encrypted:
        raise RuntimeError(f"{path} не зашифрована, а DB_ENCRYPTION_KEY задан. Остановите сервис и выполните "
                           "python -m app.cli encrypt-db (или уберите ключ)")
    if not key and encrypted:
        raise RuntimeError(f"{path} зашифрована (SQLCipher), а DB_ENCRYPTION_KEY не задан")


def make_engine(url: str, key: str | None = None, foreign_keys: bool = True):
    key = config.db_encryption_key if key is None else key
    if path := sqlite_path(url):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    kwargs = {"module": _dbapi(key)} if key else {}
    eng = create_engine(url, connect_args={"check_same_thread": False}, **kwargs)

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _):
        _apply_pragmas(dbapi_conn, key, foreign_keys)

    return eng


def secure_db_files(url: str | None = None) -> None:
    """Права 0700 на каталог БД и 0600 на файлы: в БД хеши паролей и журнал входов с IP."""
    path = sqlite_path(url or config.database_url)
    if not path or not path.exists():
        return
    os.chmod(path.parent, 0o700)
    for f in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if f.exists():
            os.chmod(f, 0o600)


def checkpoint_wal(eng=None) -> None:
    """Переносит WAL в основной файл и обрезает -wal до нуля: старые версии строк
    (в т.ч. удалённых статей) не лежат в -wal неограниченно долго (аудит S-07)."""
    with (eng or engine).connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")


engine = make_engine(config.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
