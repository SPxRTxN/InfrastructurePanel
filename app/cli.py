"""Команды обслуживания: python -m app.cli <команда>."""
import argparse
import getpass
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app import migrate
from app.config import config
from app.db import (Base, SessionLocal, _key_pragma, check_db_encryption, connect_raw, is_encrypted_file,
                    make_engine, secure_db_files, sqlite_path)
from app.models import Group, User
from app.services import backup as backup_service
from app.services.backup import _create_private, _wipe
from app.services.permissions import ADMIN_GROUP, ensure_default_groups
from app.services.security import MIN_PASSWORD_LEN, hash_password
from app.services.seed import seed


def create_admin() -> int:
    if not sys.stdin.isatty():
        # Без терминала getpass не может скрыть ввод, а input() падает на EOF
        print("Запустите команду в интерактивном терминале: логин и пароль вводятся с клавиатуры")
        return 1
    username = input("Логин администратора: ").strip()
    if not username:
        print("Логин не может быть пустым")
        return 1
    password = getpass.getpass("Пароль: ")
    if len(password) < MIN_PASSWORD_LEN:
        print(f"Пароль должен быть не короче {MIN_PASSWORD_LEN} символов")
        return 1
    if password != getpass.getpass("Повторите пароль: "):
        print("Пароли не совпадают")
        return 1
    with SessionLocal() as db:
        ensure_default_groups(db)
        if db.scalar(select(User).where(User.username == username)):
            print("Пользователь с таким логином уже существует")
            return 1
        admin_group = db.scalar(select(Group).where(Group.name == ADMIN_GROUP))
        db.add(User(username=username, password_hash=hash_password(password), groups=[admin_group]))
        db.commit()
    print(f"Администратор {username} создан")
    return 0


def run_seed() -> int:
    with SessionLocal() as db:
        created = seed(db)
    print("Начальные данные: создано серверов {servers}, устройств {devices}, связей {links}".format(**created))
    return 0


def run_guide_sync() -> int:
    from app.services import guide

    with SessionLocal() as db:
        stats = guide.sync(db)
    print("Руководство: создано {created}, обновлено {updated}, без изменений {unchanged}".format(**stats))
    return 0


def agent_release_add(path: str, notes: str) -> int:
    """Добавить архив агента (.tar.gz) в каталог версий — то же, что загрузка на странице «Агенты»."""
    from app.services import agent_updates

    try:
        data = Path(path).read_bytes()
    except OSError as e:
        print(f"Не прочитать {path}: {e.strerror}")
        return 1
    with SessionLocal() as db:
        try:
            release = agent_updates.add_release(db, data, "", notes, None)
        except agent_updates.ReleaseError as e:
            print(f"Архив не принят: {e}")
            return 1
    print(f"Версия {release.version} добавлена, sha256 {release.sha256}")
    return 0


def backup(target_dir: str | None, keep: int, age_recipient: str) -> int:
    """Горячая копия, проверка, шифрование, ротация — см. app/services/backup.py.

    С DB_ENCRYPTION_KEY копия зашифрована тем же ключом. С публичным ключом age копия дополнительно
    шифруется в .db.age: расшифровать её может только владелец приватного ключа, даже не сервер (аудит S-04).
    """
    src = sqlite_path(config.database_url)
    out_dir = Path(target_dir or config.backup_dir or (src.parent if src else Path(".")) / "backups")
    res = backup_service.make_backup(src, out_dir, keep, age_recipient)
    if not res.ok:
        print(res.error)
        return 1
    print(f"Бэкап: {res.path} ({res.size // 1024} КБ), удалено старых: {res.deleted}")
    if age_recipient:
        print("Зашифрован age: восстановление — age -d -i <приватный ключ> файл.db.age > homelab.db")
    if config.db_encryption_key:
        print("Копия зашифрована DB_ENCRYPTION_KEY — без этого ключа её не открыть")
    print("Ключи (SECRET_KEY, DB_ENCRYPTION_KEY, PASSWORD_PEPPER) храните отдельно от бэкапов")
    return 0


def _row_counts(eng) -> dict[str, int]:
    """Число строк в каждой таблице приложения — сверка до и после шифрования."""
    with eng.connect() as conn:
        counts = {t.name: conn.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}
    eng.dispose()
    return counts


def encrypt_db() -> int:
    """Однократно переводит существующую открытую БД в зашифрованную SQLCipher. Сервис должен быть остановлен."""
    src = sqlite_path(config.database_url)
    if not config.db_encryption_key:
        print("Сначала задайте DB_ENCRYPTION_KEY (64 hex): python3 -c \"import secrets; print(secrets.token_hex(32))\"")
        return 1
    if not src or not src.exists():
        print(f"БД не найдена: {config.database_url} — новая БД будет создана зашифрованной сама")
        return 1
    if is_encrypted_file(src):
        print("БД уже зашифрована")
        return 0
    import sqlcipher3

    _key_pragma(config.db_encryption_key)  # проверка формата ключа до любых изменений
    with closing(sqlite3.connect(src)) as plain:
        plain.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # всё из -wal — в основной файл
    counts = _row_counts(make_engine(f"sqlite:///{src}", key=""))
    enc = src.with_name(src.name + ".enc-tmp")
    _create_private(enc)
    # Открытую БД открываем модулем SQLCipher без ключа и экспортируем в зашифрованную копию
    with closing(sqlcipher3.connect(src)) as conv:
        conv.execute("ATTACH DATABASE ? AS enc KEY ?", (str(enc), f"x'{config.db_encryption_key}'"))
        conv.execute("SELECT sqlcipher_export('enc')")
        conv.execute("DETACH DATABASE enc")
    with closing(connect_raw(enc)) as check:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    ok = ok and _row_counts(make_engine(f"sqlite:///{enc}")) == counts
    if not ok:
        _wipe(enc)
        print("Зашифрованная копия не совпала с исходной — ничего не изменено")
        return 1
    for side in ("-wal", "-shm"):
        _wipe(src.with_name(src.name + side))
    _wipe(src)
    enc.rename(src)
    print(f"Готово: {src} зашифрована, строк перенесено: {sum(counts.values())}")
    for bk_dir in {src.parent / "backups", Path(config.backup_dir or src.parent / "backups")}:
        plain_backups = [b for b in bk_dir.glob("homelab-*.db") if not is_encrypted_file(b)]
        if plain_backups:
            print(f"Внимание: {len(plain_backups)} старых бэкапов в {bk_dir} не зашифрованы — "
                  "удалите их или перенесите в безопасное место")
    return 0


def serve(host: str, port: int) -> int:
    import uvicorn

    try:
        Fernet(config.secret_key)
    except ValueError:
        # Без ключа сохранение SSH-ключа падает с 500, а со сменённым ключом старые пути не расшифровываются
        print("SECRET_KEY в .env пуст или неверен. Сгенерируйте:\n"
              "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
        return 1
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"Внимание: дашборд доступен из сети по http://<IP этого ПК>:{port} — трафик не шифруется")
    # Один воркер: планировщик работает внутри процесса и не должен дублироваться
    # ws_max_size: сообщения агентов меньше 256 КБ — больший фрейм отбрасывается до разбора
    uvicorn.run("app.main:app", host=host, port=port, proxy_headers=True, ws_max_size=1024 * 1024)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-admin", help="создать администратора (пароль вводится интерактивно)")
    sub.add_parser("seed", help="загрузить начальные серверы и топологию (повторно не дублирует)")
    sub.add_parser("guide-sync", help="обновить руководство в Confluence (пространство GUIDE) из app/guide/*.md")
    p_serve = sub.add_parser("serve", help="запустить веб-сервер (адрес и порт — HOST/PORT в .env)")
    p_serve.add_argument("--host", default=config.host)
    p_serve.add_argument("--port", type=int, default=config.port)
    p_backup = sub.add_parser("backup", help="горячий бэкап БД (по умолчанию в <каталог БД>/backups)")
    p_backup.add_argument("--dir", help="каталог для копий")
    p_backup.add_argument("--keep", type=int, default=14, help="сколько последних копий хранить (0 — все)")
    p_backup.add_argument("--age-recipient", default=config.backup_age_recipient,
                          help="публичный ключ age (age1…); по умолчанию BACKUP_AGE_RECIPIENT")
    p_backup.add_argument("--no-age", action="store_true",
                          help="без age (для отката в deploy.sh: копию нужно восстановить без приватного ключа)")
    p_agent = sub.add_parser("agent-release", help="каталог версий агента для обновления узлов из панели")
    p_agent.add_argument("action", choices=["add"])
    p_agent.add_argument("file", help="архив агента .tar.gz (с homelab_agent/__init__.py)")
    p_agent.add_argument("--notes", default="", help="заметки к версии")
    sub.add_parser("encrypt-db", help="зашифровать существующую открытую БД ключом DB_ENCRYPTION_KEY")
    args = parser.parse_args(argv)

    if args.cmd == "encrypt-db":
        return encrypt_db()  # до миграций: открытая БД с заданным ключом ещё не открывается
    try:
        check_db_encryption()
    except (RuntimeError, ValueError) as e:
        print(e)
        return 1
    migrate.upgrade()
    secure_db_files()
    if args.cmd == "create-admin":
        return create_admin()
    if args.cmd == "seed":
        return run_seed()
    if args.cmd == "agent-release":
        return agent_release_add(args.file, args.notes)
    if args.cmd == "guide-sync":
        return run_guide_sync()
    if args.cmd == "serve":
        return serve(args.host, args.port)
    if args.cmd == "backup":
        return backup(args.dir, args.keep, "" if args.no_age else args.age_recipient)
    return 1


if __name__ == "__main__":
    sys.exit(main())
