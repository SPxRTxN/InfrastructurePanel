"""Создаёт каталог с ключами для compose.secrets.yaml / *_FILE. Ключи на экран не выводятся.

    .venv/bin/python tools/setup_secrets.py /mnt/data/homelab-secrets

Создаёт (права каталога 0700, файлов 0600; существующие файлы НЕ перезаписываются — иначе данные потеряны):
  secret_key            — переносится из текущего SECRET_KEY (.env), если он задан, иначе новый
  db_encryption_key     — 64 hex для SQLCipher
  password_pepper       — 64 hex
  first_admin_password  — пустой (на существующей БД не нужен)
Печатает отпечатки (первые 8 символов SHA-256) — по ним сверяют копию в менеджере паролей,
и строки, которые нужно добавить в .env.
"""
import hashlib
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

from app.config import config  # noqa: E402


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8] if value else "пусто"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 1
    target = Path(argv[0]).resolve()
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    values = {
        "secret_key": config.secret_key or Fernet.generate_key().decode(),
        "db_encryption_key": secrets.token_hex(32),
        "password_pepper": secrets.token_hex(32),
        "first_admin_password": "",
    }
    for name, value in values.items():
        path = target / name
        if path.exists():
            print(f"  {name:<22} уже есть — не трогаю (отпечаток {fingerprint(path.read_text().strip())})")
            continue
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value + ("\n" if value else ""))
        note = " (перенесён из .env)" if name == "secret_key" and config.secret_key else ""
        print(f"  {name:<22} создан{note}, отпечаток {fingerprint(value)}")
    print(f"\nКаталог: {target}")
    print("Сохраните копию каждого файла в менеджере паролей: без них данные и бэкапы не восстановить.")
    print("\nДобавьте в .env (Docker):")
    print("  COMPOSE_FILE=compose.yaml:compose.secrets.yaml")
    print(f"  SECRETS_DIR={target}")
    print("Без Docker (app.cli serve) — вместо этого:")
    for name in ("secret_key", "db_encryption_key", "password_pepper"):
        print(f"  {name.upper()}_FILE={target / name}")
    print("и удалите из .env строку SECRET_KEY=… (ключ теперь в файле).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
