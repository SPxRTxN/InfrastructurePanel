"""Настройки окружения из .env. Всё, что зависит от развёртывания, — здесь."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# Тесты не должны видеть боевой .env: иначе берут настоящие ключи и пишут бэкапы в настоящий BACKUP_DIR
if not os.getenv("HOMELAB_IGNORE_DOTENV"):
    load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _secret(name: str) -> str:
    """Секрет из файла <NAME>_FILE (Docker secrets, /run/secrets/…) или из переменной <NAME>.

    Файл лучше: переменные окружения видны в `docker inspect` и /proc/<pid>/environ (аудит S-05).
    """
    path = os.getenv(f"{name}_FILE")
    if path:
        return Path(path).read_text().strip()
    return os.getenv(name, "")


@dataclass
class Config:
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'homelab.db'}")
    secret_key: str = _secret("SECRET_KEY")
    # Пароль администратора по умолчанию на пустой БД; пусто — admin (аудит S-02)
    first_admin_password: str = _secret("FIRST_ADMIN_PASSWORD")
    # 64 hex: шифрование всей БД SQLCipher; пусто — обычный SQLite (аудит S-01)
    db_encryption_key: str = _secret("DB_ENCRYPTION_KEY")
    # «Перец» паролей: HMAC перед argon2id, хранится вне БД; пусто — выключен (аудит S-08)
    password_pepper: str = _secret("PASSWORD_PEPPER")
    # Публичный ключ age (age1…): бэкапы шифруются им, расшифровать может только владелец приватного ключа (S-04)
    backup_age_recipient: str = os.getenv("BACKUP_AGE_RECIPIENT", "")
    # Каталог бэкапов (лучше на другом диске, чем БД); пусто — <каталог БД>/backups
    backup_dir: str = os.getenv("BACKUP_DIR", "")
    cookie_secure: bool = _bool("COOKIE_SECURE", False)
    session_hours: int = int(os.getenv("SESSION_HOURS", "12"))
    scheduler_enabled: bool = _bool("SCHEDULER_ENABLED", True)
    # Адрес для `python -m app.cli serve`: 127.0.0.1 — только этот ПК, 0.0.0.0 — вся локальная сеть
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))
    # Каталог проекта на хосте — для подсказок с командами (push-agent и т.п.); в Docker внутри контейнера он другой
    project_dir: str = os.getenv("PROJECT_DIR", str(BASE_DIR))
    # Репозиторий GitHub с релизами (владелец/имя) — значение по умолчанию для «Настройки → Обновление»
    github_repo: str = os.getenv("GITHUB_REPO", "SPxRTxN/InfrastructurePanel")


config = Config()
