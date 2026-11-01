"""Автоматическая часть аудита безопасности и хранения данных.

Запуск:  .venv/bin/python tools/audit.py [--offline] [--no-tests] [--write-vendor-hashes]
Код выхода: 0 — нет FAIL, 1 — есть FAIL (подходит для CI). WARN не валит запуск.
Ручная часть — чек-лист в README, раздел «Безопасность».
"""
import argparse
import hashlib
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
VENDOR = ROOT / "app" / "static" / "vendor"
HASHES = VENDOR / "SHA256SUMS"

results: list[tuple[str, str, str]] = []


def report(level: str, check: str, detail: str = "") -> None:
    results.append((level, check, detail))


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


# ---------- Репозиторий ----------

def check_git() -> None:
    tracked = run(["git", "ls-files"]).stdout.splitlines()
    bad = [f for f in tracked if f == ".env" or f.endswith((".db", ".sqlite", ".pem", ".key")) or f.startswith("data/")]
    report("FAIL" if bad else "OK", "Секреты и БД не в git", ", ".join(bad))
    ignore = (ROOT / ".gitignore").read_text().split()
    missing = [p for p in (".env", "data/", "*.db") if p not in ignore]
    report("FAIL" if missing else "OK", ".gitignore закрывает .env, data/, *.db", ", ".join(missing))


# ---------- Окружение ----------

def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def check_env() -> None:
    from app.config import config

    env = ROOT / ".env"
    if not env.exists():
        report("WARN", ".env существует", "нет .env — используются значения по умолчанию")
    else:
        mode = _mode(env)
        report("OK" if mode & 0o077 == 0 else "WARN", ".env доступен только владельцу", f"права {oct(mode)}, нужно 0o600")
    if not config.secret_key:
        report("WARN", "SECRET_KEY задан", "пусто: app.cli serve не запустится")
    else:
        from cryptography.fernet import Fernet
        try:
            Fernet(config.secret_key.encode())
            report("OK", "SECRET_KEY — корректный ключ Fernet")
        except ValueError:
            report("FAIL", "SECRET_KEY — корректный ключ Fernet", "ключ не декодируется")
    report("OK" if config.cookie_secure else "WARN", "COOKIE_SECURE=true (для прода за HTTPS)",
           "" if config.cookie_secure else "выключено — допустимо только для локального http")
    url = config.database_url
    rel = url.startswith("sqlite:///") and not url.startswith("sqlite:////")
    report("WARN" if rel else "OK", "DATABASE_URL — абсолютный путь",
           f"{url} зависит от рабочего каталога процесса" if rel else url)
    in_env = [n for n in ("SECRET_KEY", "DB_ENCRYPTION_KEY", "PASSWORD_PEPPER")
              if os.getenv(n) and not os.getenv(f"{n}_FILE")]
    report("OK" if not in_env else "WARN", "Секреты из файлов (*_FILE), не из окружения",
           f"{', '.join(in_env)} — в .env/окружении рядом с данными" if in_env else "")


# ---------- База данных ----------

def check_db() -> None:
    from app.config import config

    if not config.database_url.startswith("sqlite:///"):
        report("WARN", "БД — SQLite", "проверки хранения рассчитаны на SQLite")
        return
    path = Path(config.database_url.removeprefix("sqlite:///"))
    if not path.exists():
        report("WARN", "Файл БД существует", str(path))
        return
    mode, dmode = _mode(path), _mode(path.parent)
    report("OK" if mode & 0o077 == 0 else "WARN", "Файл БД доступен только владельцу", f"{path.name}: {oct(mode)}, нужно 0o600")
    report("OK" if dmode & 0o077 == 0 else "WARN", "Каталог БД закрыт от других", f"{path.parent}: {oct(dmode)}, нужно 0o700")

    from app.db import check_db_encryption, connect_raw, is_encrypted_file

    encrypted = is_encrypted_file(path)
    report("OK" if encrypted else "WARN", "БД зашифрована (SQLCipher)",
           "" if encrypted else "открытый текст: задайте DB_ENCRYPTION_KEY и выполните app.cli encrypt-db")
    # Все места, где могут лежать копии: BACKUP_DIR, рядом с БД и старый data/ проекта после переноса БД
    bk_dirs = {Path(config.backup_dir) if config.backup_dir else path.parent / "backups",
               path.parent / "backups", ROOT / "data" / "backups"}
    plain_bk = [b for d in sorted(bk_dirs) for b in d.glob("homelab-*.db*")
                if b.suffix != ".age" and not is_encrypted_file(b)]
    old_db = ROOT / "data" / "homelab.db"
    if old_db != path and old_db.exists():
        plain_bk.append(old_db)
    report("OK" if not plain_bk else "WARN", "Бэкапы и старые копии БД зашифрованы",
           f"{len(plain_bk)} открытых: {', '.join(str(b) for b in plain_bk[:3])}" if plain_bk else "")
    try:
        check_db_encryption()
    except (RuntimeError, ValueError) as e:
        report("FAIL", "Ключ БД подходит к файлу", str(e))
        return

    con = connect_raw(path)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        report("OK" if integrity == "ok" else "FAIL", "PRAGMA integrity_check", integrity)
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        report("OK" if not fk else "FAIL", "PRAGMA foreign_key_check", f"{len(fk)} нарушений" if fk else "")

        from alembic.script import ScriptDirectory
        from app.migrate import alembic_config
        head = ScriptDirectory.from_config(alembic_config()).get_current_head()
        current = con.execute("SELECT version_num FROM alembic_version").fetchone()
        current = current[0] if current else None
        report("OK" if current == head else "WARN", "Схема БД на последней миграции", f"БД {current}, код {head}")

        journal = con.execute("PRAGMA journal_mode").fetchone()[0]
        report("OK" if journal == "wal" else "WARN", "journal_mode=WAL",
               "" if journal == "wal" else f"сейчас {journal}: возможны 'database is locked'")

        now = datetime.now(UTC).replace(tzinfo=None)
        growth = {
            "login_attempts старше 90 дней": ("login_attempts", "created_at", now - timedelta(days=90)),
            "notifications старше 90 дней": ("notifications", "created_at", now - timedelta(days=90)),
            "просроченные sessions": ("sessions", "expires_at", now),
            "server_metrics старше 24 ч": ("server_metrics", "ts", now - timedelta(hours=25)),
        }
        for label, (table, col, before) in growth.items():
            n = con.execute(f"SELECT count(*) FROM {table} WHERE {col} < ?", (before.isoformat(" "),)).fetchone()[0]  # nosec B608 — имена из словаря выше
            report("OK" if n == 0 else "WARN", f"Хранение: {label}", f"{n} строк — нет автоочистки" if n else "")
        size = path.stat().st_size / 1024 / 1024
        report("OK" if size < 500 else "WARN", "Размер БД", f"{size:.1f} МБ")

        # Регрессия 2026-10-31: миграция стёрла user_groups — пользователи остались без прав
        no_group = [r[0] for r in con.execute(
            "SELECT username FROM users WHERE is_active = 1 AND id NOT IN (SELECT user_id FROM user_groups)")]
        report("OK" if not no_group else "WARN", "У активных пользователей есть группа",
               f"без группы (видят только «Главную»): {', '.join(no_group)}" if no_group else "")
        admins = con.execute(
            "SELECT count(DISTINCT u.id) FROM users u JOIN user_groups ug ON ug.user_id = u.id "
            "JOIN group_permissions gp ON gp.group_id = ug.group_id "
            "WHERE u.is_active = 1 AND gp.perm = 'users.manage'").fetchone()[0]
        report("OK" if admins else "FAIL", "Есть активный администратор (users.manage)",
               "" if admins else "никто не может управлять пользователями — app.cli create-admin")

        from app.services.security import PEPPER_PREFIX
        hashes = [r[0] for r in con.execute("SELECT password_hash FROM users")]
        weak = [h for h in hashes if "$argon2id$" not in h]
        report("OK" if not weak else "FAIL", "Пароли — argon2id", f"{len(weak)} хешей другого формата" if weak else "")
        if config.password_pepper:
            left = sum(not h.startswith(PEPPER_PREFIX) for h in hashes)
            report("OK" if not left else "WARN", "Перец паролей", f"{left} хешей без перца — обновятся при входе" if left else "")
        else:
            report("WARN", "Перец паролей", "PASSWORD_PEPPER не задан: по утёкшему файлу БД слабые пароли перебираются")
    finally:
        con.close()


# ---------- Статика ----------

def vendor_hashes() -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(VENDOR.iterdir()) if p.is_file() and p.name != HASHES.name}


def check_vendor() -> None:
    if not HASHES.exists():
        report("FAIL", "Целостность JS-библиотек", "нет SHA256SUMS: tools/audit.py --write-vendor-hashes")
        return
    expected = dict(reversed(line.split("  ", 1)) for line in HASHES.read_text().splitlines() if line)
    actual = vendor_hashes()
    changed = [n for n in actual if expected.get(n) != actual[n]] + [n for n in expected if n not in actual]
    report("FAIL" if changed else "OK", "Целостность JS-библиотек (SHA256SUMS)", ", ".join(changed))


# ---------- Внешние инструменты ----------

def check_tools(offline: bool, tests: bool) -> None:
    py = sys.executable
    r = run([py, "-m", "bandit", "-r", "app", "-q", "-ll"])
    if "No module named" in r.stderr:
        report("WARN", "bandit (статический анализ)", "не установлен: pip install -r requirements-dev.txt")
    else:
        report("OK" if r.returncode == 0 else "FAIL", "bandit (статический анализ)",
               "" if r.returncode == 0 else r.stdout.strip()[-800:])
    if offline:
        report("WARN", "pip-audit (уязвимости зависимостей)", "пропущено (--offline)")
    else:
        r = run([py, "-m", "pip_audit", "-r", "requirements.txt", "--progress-spinner", "off"])
        if "No module named" in r.stderr:
            report("WARN", "pip-audit (уязвимости зависимостей)", "не установлен: pip install -r requirements-dev.txt")
        else:
            report("OK" if r.returncode == 0 else "FAIL", "pip-audit (уязвимости зависимостей)",
                   "" if r.returncode == 0 else (r.stdout + r.stderr).strip()[-800:])
    if tests:
        r = run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_security_checklist.py",
                 "tests/test_auth.py"])
        report("OK" if r.returncode == 0 else "FAIL", "Тесты безопасности (pytest)",
               "" if r.returncode == 0 else (r.stdout + r.stderr).strip()[-800:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="без pip-audit (нужна сеть)")
    ap.add_argument("--no-tests", action="store_true", help="не запускать pytest")
    ap.add_argument("--write-vendor-hashes", action="store_true", help="записать SHA256SUMS после обновления библиотек")
    args = ap.parse_args()

    if args.write_vendor_hashes:
        HASHES.write_text("".join(f"{h}  {n}\n" for n, h in vendor_hashes().items()))
        print(f"Записано: {HASHES.relative_to(ROOT)}")
        return 0

    os.environ.setdefault("SCHEDULER_ENABLED", "false")
    check_git()
    check_env()
    check_db()
    check_vendor()
    check_tools(args.offline, not args.no_tests)

    width = max(len(c) for _, c, _ in results)
    for level, check, detail in results:
        print(f"[{level:4}] {check:<{width}}  {detail}")
    fails = sum(1 for r in results if r[0] == "FAIL")
    warns = sum(1 for r in results if r[0] == "WARN")
    print(f"\nИтого: FAIL {fails}, WARN {warns}, OK {len(results) - fails - warns}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
