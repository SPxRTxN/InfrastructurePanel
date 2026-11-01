#!/usr/bin/env bash
# Нативная установка Homelab Dashboard как службы systemd (без Docker).
# Raspberry Pi OS bookworm (arm64, Python 3.11), Debian 12+, Ubuntu 24.04+ (amd64/arm64).
#
#   sudo deploy/native/install.sh                       # код — из этого репозитория
#   sudo deploy/native/install.sh --git URL [--ref REF] # код — git clone URL (ветка/тег REF)
#   sudo deploy/native/install.sh --upgrade             # бэкап БД → новый код → миграции → перезапуск
#   опции: --memory-max 300M (потолок RAM службы), --no-apt (не ставить системные пакеты)
#
# Идемпотентен: существующие env, секреты и БД не перезаписываются. Секреты на экран не выводятся.
set -euo pipefail

APP_USER=homelab-dashboard
PREFIX=/opt/homelab-dashboard
DATA=/var/lib/homelab-dashboard
ETC=/etc/homelab-dashboard
SECRETS="$ETC/secrets"
ENV_FILE="$ETC/env"
UNIT_NAME=homelab-dashboard.service
UNIT="/etc/systemd/system/$UNIT_NAME"
DROPIN_DIR="/etc/systemd/system/$UNIT_NAME.d"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GIT_URL=""
GIT_REF=""
UPGRADE=0
APT=1
MEMORY_MAX=""
while [ $# -gt 0 ]; do
    case "$1" in
        --git) GIT_URL="${2:?--git требует URL}"; shift 2 ;;
        --ref) GIT_REF="${2:?--ref требует ветку или тег}"; shift 2 ;;
        --upgrade) UPGRADE=1; shift ;;
        --no-apt) APT=0; shift ;;
        --memory-max) MEMORY_MAX="${2:?--memory-max требует значение, например 300M}"; shift 2 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '  внимание: %s\n' "$*" >&2; }
die() { echo "ОШИБКА: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запустите через sudo"
command -v systemctl >/dev/null || die "нужен systemd"
case "$MEMORY_MAX" in ""|[0-9]*[KMG]|infinity) ;; *) die "--memory-max: число с K/M/G, например 300M" ;; esac

# Команда приложения от имени службы, с её окружением (как в юните)
app_run() {
    (
        set -a
        # shellcheck disable=SC1090
        . "$ENV_FILE"
        set +a
        export HOME="$DATA" HOMELAB_IGNORE_DOTENV=1 PYTHONDONTWRITEBYTECODE=1
        cd "$PREFIX"
        exec runuser -u "$APP_USER" -- "$@"
    )
}

# --- 1. Системные пакеты ---
if [ "$APT" -eq 1 ] && command -v apt-get >/dev/null; then
    say "Системные пакеты"
    # Все зависимости — готовые сборки (tools/check_wheels.py), компилятор не нужен ни на amd64, ни на arm64
    pkgs=(python3 python3-venv tzdata ca-certificates)
    [ -n "$GIT_URL" ] && pkgs+=(git)
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${pkgs[@]}" >/dev/null
fi
command -v python3 >/dev/null || die "нужен python3"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || die "нужен Python 3.11+"
python3 -c 'import venv, ensurepip' 2>/dev/null || die "нужен модуль venv: apt install python3-venv"

# --- 2. Исходники ---
TMP_SRC=""
cleanup() { [ -n "$TMP_SRC" ] && rm -r -f -- "$TMP_SRC"; return 0; }
trap cleanup EXIT
if [ -n "$GIT_URL" ]; then
    command -v git >/dev/null || die "нужен git"
    TMP_SRC="$(mktemp -d)"
    say "git clone $GIT_URL ${GIT_REF:+(ref $GIT_REF)}"
    git clone --quiet --depth 1 ${GIT_REF:+--branch "$GIT_REF"} -- "$GIT_URL" "$TMP_SRC/repo"
    SRC="$TMP_SRC/repo"
else
    SRC="$(cd "$HERE/../.." && pwd)"
fi
for f in app/cli.py migrations/env.py alembic.ini requirements.txt requirements.lock tools/setup_secrets.py; do
    [ -e "$SRC/$f" ] || die "в $SRC нет $f — это не репозиторий Homelab Dashboard"
done
# Файлы нативной установки — из исходников, а если их там нет (старая ветка) — рядом со скриптом
NATIVE="$SRC/deploy/native"
[ -f "$NATIVE/homelab-dashboard.service" ] || NATIVE="$HERE"

# --- 3. Пользователь и каталоги ---
say "Пользователь $APP_USER и каталоги"
if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -m 0755 -o root -g root "$PREFIX"
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$DATA"
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$DATA/.ssh"
# /etc/homelab-dashboard: env читает только root (systemd), secrets/ — только служба
install -d -m 0750 -o root -g "$APP_USER" "$ETC"
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$SECRETS"

# --- 4. Бэкап перед обновлением (старым кодом, пока он на месте) ---
if [ "$UPGRADE" -eq 1 ]; then
    [ -x "$PREFIX/.venv/bin/python" ] && [ -f "$ENV_FILE" ] || die "--upgrade: установка не найдена, запустите без --upgrade"
    db_path="$(sed -n 's#^DATABASE_URL=sqlite:///##p' "$ENV_FILE" | tail -n 1)"
    if [ -n "$db_path" ] && [ -f "$db_path" ]; then
        say "Бэкап БД перед обновлением"
        # --no-age: копию для отката можно восстановить на узле без приватного ключа age
        app_run "$PREFIX/.venv/bin/python" -m app.cli backup --no-age || die "бэкап не удался — обновление остановлено"
    else
        warn "БД ${db_path:-?} не найдена — бэкап пропущен"
    fi
    systemctl stop "$UNIT_NAME" || true
fi

# --- 5. Код ---
say "Код в $PREFIX"
for d in app migrations tools; do
    rm -r -f -- "$PREFIX/$d.new"
    cp -r -- "$SRC/$d" "$PREFIX/$d.new"
    find "$PREFIX/$d.new" -name '__pycache__' -type d -prune -exec rm -r -f -- {} +
    rm -r -f -- "$PREFIX/$d"
    mv -- "$PREFIX/$d.new" "$PREFIX/$d"
done
for f in alembic.ini requirements.txt requirements.lock README.md; do
    install -m 0644 -o root -g root "$SRC/$f" "$PREFIX/$f"
done
install -d -m 0755 "$PREFIX/deploy/native"
for f in README.md env.example homelab-dashboard.service install.sh homelab-dashboard-cli; do
    install -m 0644 -o root -g root "$NATIVE/$f" "$PREFIX/deploy/native/$f"
done
# Обёртка для app.cli (backup, create-admin, encrypt-db) с окружением службы
install -m 0755 -o root -g root "$NATIVE/homelab-dashboard-cli" /usr/local/sbin/homelab-dashboard-cli
chown -R root:root "$PREFIX/app" "$PREFIX/migrations" "$PREFIX/tools"
chmod -R u=rwX,go=rX "$PREFIX/app" "$PREFIX/migrations" "$PREFIX/tools"

# --- 6. venv и зависимости (фиксированные версии из requirements.lock) ---
say "Виртуальное окружение и зависимости"
[ -x "$PREFIX/.venv/bin/python" ] || python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir --upgrade pip
# Только готовые сборки: без компилятора на узле; нет wheel под эту ОС/Python — понятная ошибка pip сразу
"$PREFIX/.venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir --only-binary=:all: \
    -r "$PREFIX/requirements.txt" -c "$PREFIX/requirements.lock"

# --- 7. env и секреты (только если их ещё нет) ---
say "Конфиг $ENV_FILE и секреты $SECRETS"
if [ ! -f "$ENV_FILE" ]; then
    install -m 0600 -o root -g root "$NATIVE/env.example" "$ENV_FILE"
    echo "  создан $ENV_FILE"
else
    chown root:root "$ENV_FILE"
    chmod 0600 "$ENV_FILE"
    echo "  $ENV_FILE уже есть — не трогаю"
fi
# Пароль первого входа admin — случайный, а не admin/admin (узел может быть виден из сети)
if [ ! -e "$SECRETS/first_admin_password" ]; then
    ( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(18))' > "$SECRETS/first_admin_password" )
    chown "$APP_USER:$APP_USER" "$SECRETS/first_admin_password"
fi
# setup_secrets.py создаёт недостающие ключи (0600), существующие не трогает; печатает отпечатки и
# подсказки для Docker — показываем только строки с отпечатками
secrets_log="$(mktemp)"
if ! ( cd "$PREFIX" && HOMELAB_IGNORE_DOTENV=1 runuser -u "$APP_USER" -- \
        "$PREFIX/.venv/bin/python" tools/setup_secrets.py "$SECRETS" ) > "$secrets_log" 2>&1; then
    cat "$secrets_log" >&2
    rm -f -- "$secrets_log"
    die "не удалось создать секреты"
fi
grep -E '^\s+[a-z_]+\s' "$secrets_log" || true
rm -f -- "$secrets_log"
chown -R "$APP_USER:$APP_USER" "$SECRETS"
chmod 0700 "$SECRETS"
find "$SECRETS" -type f -exec chmod 0600 {} +

# --- 8. Миграции ---
say "Миграции БД (alembic upgrade head)"
app_run "$PREFIX/.venv/bin/alembic" -c "$PREFIX/alembic.ini" upgrade head
find "$DATA" -maxdepth 1 -name 'homelab.db*' -exec chmod 0600 {} +

# --- 9. Юнит ---
say "Служба systemd"
install -m 0644 -o root -g root "$NATIVE/homelab-dashboard.service" "$UNIT"
install -d -m 0755 "$DROPIN_DIR"
if [ -n "$MEMORY_MAX" ] || [ ! -f "$DROPIN_DIR/10-resources.conf" ]; then
    if [ -z "$MEMORY_MAX" ]; then
        mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
        # Pi 4B 1 ГБ: дашборд ~120–180 МБ; потолок, чтобы при утечке не задушить систему
        if [ "$mem_kb" -le 1300000 ]; then MEMORY_MAX=320M; elif [ "$mem_kb" -le 2600000 ]; then MEMORY_MAX=512M; else MEMORY_MAX=768M; fi
    fi
    printf '# Создано install.sh; поменять: install.sh --memory-max 400M\n[Service]\nMemoryMax=%s\n' \
        "$MEMORY_MAX" > "$DROPIN_DIR/10-resources.conf"
    echo "  потолок памяти службы: $MEMORY_MAX ($DROPIN_DIR/10-resources.conf)"
fi
systemctl daemon-reload
systemctl enable --quiet "$UNIT_NAME"
systemctl restart "$UNIT_NAME"

port="$(sed -n 's/^PORT=\([0-9]*\).*/\1/p' "$ENV_FILE" | tail -n 1)"
host="$(sed -n 's/^HOST=\(.*\)$/\1/p' "$ENV_FILE" | tail -n 1)"
say "Готово: служба $UNIT_NAME запущена, http://${host:-127.0.0.1}:${port:-8000}"
cat <<EOF
  Журнал:          journalctl -u $UNIT_NAME -f
  Первый вход:     логин admin, пароль — sudo cat $SECRETS/first_admin_password
                   (вход потребует сменить логин и пароль)
  Настройки:       sudoedit $ENV_FILE, затем sudo systemctl restart $UNIT_NAME
  Бэкап вручную:   sudo homelab-dashboard-cli backup
  Свой админ:      sudo homelab-dashboard-cli create-admin
  Сохраните копии файлов $SECRETS/* в менеджере паролей: без них БД и бэкапы не открыть.
EOF
