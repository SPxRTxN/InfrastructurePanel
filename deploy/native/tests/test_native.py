"""Статические проверки нативной установки: синтаксис скриптов, юнит, env-шаблон против app/config.py."""
import configparser
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NATIVE = Path(__file__).resolve().parent.parent
REPO = NATIVE.parent.parent
SCRIPTS = ["install.sh", "homelab-dashboard-cli"]


def unit_service():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(NATIVE / "homelab-dashboard.service", encoding="utf-8")
    return cp


def env_example() -> dict[str, str]:
    out = {}
    for line in (NATIVE / "env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, _, val = line.partition("=")
            out[key] = val
    return out


def commented_env_keys() -> set[str]:
    text = (NATIVE / "env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^# ([A-Z][A-Z0-9_]+)=", text, re.M))


def config_names() -> set[str]:
    """Имена переменных, которые читает app/config.py (+ *_FILE для секретов)."""
    text = (REPO / "app" / "config.py").read_text(encoding="utf-8")
    names = set(re.findall(r'os\.getenv\("([A-Z0-9_]+)"', text))
    names |= set(re.findall(r'_bool\("([A-Z0-9_]+)"', text))
    for secret in re.findall(r'_secret\("([A-Z0-9_]+)"\)', text):
        names |= {secret, f"{secret}_FILE"}
    return names


@pytest.mark.skipif(not shutil.which("bash"), reason="нет bash")
@pytest.mark.parametrize("script", SCRIPTS)
def test_bash_syntax(script):
    res = subprocess.run(["bash", "-n", str(NATIVE / script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize("script", SCRIPTS)
def test_scripts_strict_mode(script):
    text = (NATIVE / script).read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text
    assert 'id -u)" -eq 0' in text  # только через sudo


def test_install_paths_and_permissions():
    t = (NATIVE / "install.sh").read_text(encoding="utf-8")
    assert "APP_USER=homelab-dashboard" in t and "PREFIX=/opt/homelab-dashboard" in t
    assert "DATA=/var/lib/homelab-dashboard" in t and "ETC=/etc/homelab-dashboard" in t
    assert 'install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$DATA"' in t
    assert 'install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$SECRETS"' in t
    assert 'install -m 0600 -o root -g root "$NATIVE/env.example" "$ENV_FILE"' in t
    assert "useradd --system" in t and "--shell /usr/sbin/nologin" in t


def test_install_does_not_overwrite_and_is_idempotent():
    t = (NATIVE / "install.sh").read_text(encoding="utf-8")
    assert 'if [ ! -f "$ENV_FILE" ]' in t
    assert 'if [ ! -e "$SECRETS/first_admin_password" ]' in t
    assert 'if ! id "$APP_USER"' in t
    assert '[ -x "$PREFIX/.venv/bin/python" ] || python3 -m venv' in t
    # БД не удаляется и не копируется поверх; секреты — только через setup_secrets.py (O_EXCL)
    assert not re.search(r"rm [^\n]*\$DATA", t)
    assert not re.search(r"rm [^\n]*\$SECRETS", t)
    assert not re.search(r"rm [^\n]*\$ENV_FILE", t)
    assert "tools/setup_secrets.py" in t


def test_install_pip_alembic_upgrade_backup():
    t = (NATIVE / "install.sh").read_text(encoding="utf-8")
    assert '-r "$PREFIX/requirements.txt" -c "$PREFIX/requirements.lock"' in t
    assert "alembic\" -c \"$PREFIX/alembic.ini\" upgrade head" in t
    assert "--upgrade" in t and "app.cli backup" in t
    # Бэкап — до замены кода и до миграций
    assert t.index("app.cli backup") < t.index('cp -r -- "$SRC/$d"') < t.index("upgrade head")
    assert "git clone" in t and "--git" in t


def test_secrets_not_printed():
    t = (NATIVE / "install.sh").read_text(encoding="utf-8")
    # пароль первого входа пишется сразу в файл, setup_secrets печатает только отпечатки (фильтр строк)
    assert "token_urlsafe(18))' > \"$SECRETS/first_admin_password\"" in t
    assert not re.search(r"cat [^\n]*\$SECRETS/", t.split("cat <<EOF")[0])
    assert "set -x" not in t


def test_unit():
    s = unit_service()["Service"]
    assert s["User"] == s["Group"] == "homelab-dashboard"
    assert s["ExecStart"] == "/opt/homelab-dashboard/.venv/bin/python -m app.cli serve"
    assert s["EnvironmentFile"] == "/etc/homelab-dashboard/env"
    assert s["WorkingDirectory"] == "/opt/homelab-dashboard"
    assert s["Restart"] == "on-failure"
    assert "HOMELAB_IGNORE_DOTENV=1" in s["Environment"] and "HOME=/var/lib/homelab-dashboard" in s["Environment"]
    for key, val in {"NoNewPrivileges": "true", "ProtectSystem": "strict", "PrivateTmp": "true",
                     "ReadWritePaths": "/var/lib/homelab-dashboard", "PrivateDevices": "true",
                     "CapabilityBoundingSet": ""}.items():
        assert s[key] == val, key
    assert s["ProtectHome"] in ("true", "read-only")
    assert re.fullmatch(r"\d+[KMG]", s["MemoryMax"])
    assert unit_service()["Install"]["WantedBy"] == "multi-user.target"


def test_install_writes_memory_dropin_for_small_ram():
    t = (NATIVE / "install.sh").read_text(encoding="utf-8")
    assert "10-resources.conf" in t and "MemoryMax=%s" in t and "MEMORY_MAX=320M" in t


def test_env_example_names_match_app_config():
    known = config_names() | {"TZ"}
    env = env_example()
    for key in set(env) | commented_env_keys():
        assert key in known, f"{key} нет в app/config.py"
    assert env["DATABASE_URL"] == "sqlite:////var/lib/homelab-dashboard/homelab.db"
    assert env["HOST"] == "127.0.0.1" and env["PORT"] == "8000" and env["COOKIE_SECURE"] == "false"
    for name in ("SECRET_KEY", "DB_ENCRYPTION_KEY", "PASSWORD_PEPPER", "FIRST_ADMIN_PASSWORD"):
        assert env[f"{name}_FILE"] == f"/etc/homelab-dashboard/secrets/{name.lower()}"
        assert name not in env  # только файлы, без значений секретов


def test_env_example_has_no_secret_values():
    text = (NATIVE / "env.example").read_text(encoding="utf-8")
    assert not re.search(r"[0-9a-f]{32,}", text)
    assert not re.search(r"^(SECRET_KEY|DB_ENCRYPTION_KEY|PASSWORD_PEPPER|FIRST_ADMIN_PASSWORD)=", text, re.M)


def test_setup_secrets_file_names_match_env():
    src = (REPO / "tools" / "setup_secrets.py").read_text(encoding="utf-8")
    for name in ("secret_key", "db_encryption_key", "password_pepper", "first_admin_password"):
        assert f'"{name}"' in src


def test_cli_wrapper():
    t = (NATIVE / "homelab-dashboard-cli").read_text(encoding="utf-8")
    assert ". /etc/homelab-dashboard/env" in t
    assert "runuser -u homelab-dashboard --" in t and "-m app.cli \"$@\"" in t
    assert "HOMELAB_IGNORE_DOTENV=1" in t


def test_readme_covers_sections():
    t = (NATIVE / "README.md").read_text(encoding="utf-8")
    for heading in ("## Установка на Raspberry Pi 4B", "## Установка на узел администрирования", "## Обновление",
                    "## Бэкап", "## Удаление", "## Рекомендации для Raspberry Pi с 1 ГБ RAM"):
        assert heading in t
