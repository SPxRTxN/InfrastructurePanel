#!/usr/bin/env bash
# Установка и обновление homelab-agent как службы systemd. Всё лежит в одной папке /opt/homelab-agent.
#
#   sudo ./packaging/install.sh             # сервер без дисплея
#   sudo ./packaging/install.sh --display   # Raspberry Pi с OLED (Pillow + luma.oled, группа i2c)
#   sudo ./packaging/install.sh --upgrade   # обновить код и зависимости, перенести старую раскладку, перезапустить
#   sudo ./packaging/install.sh --own-python  # свой Python в /opt/homelab-agent/python (системный не нужен и не трогается)
#
# Идемпотентен: существующие agent.toml и токены не трогает. Службу не включает при первой
# установке — сначала владелец кладёт токены и проверяет конфиг (см. вывод в конце).
#
# Раскладка (подробно — README.md, «Раскладка на узле»):
#   /opt/homelab-agent            root:root 0755 (иначе агент подменил бы bin/)
#     bin/      root: uninstall-agent, guard.py, справочные файлы — агент писать не может
#     systemd/  root: юниты; в /etc/systemd/system лежат ссылки на них
#     etc/      agent.toml, token-*, panel.toml (настройки из панели), vpn/
#     app/      releases/<версия>, current, previous — переключает обновление
#     state/    отметки сторожа
set -euo pipefail

APP_USER=homelab-agent
ROOT=/opt/homelab-agent
BIN="$ROOT/bin"
ETC="$ROOT/etc"
APP="$ROOT/app"
STATE="$ROOT/state"
SYSD="$ROOT/systemd"
UNIT_DIR=/etc/systemd/system
SUDOERS_DIR=/etc/sudoers.d
SUDOERS="$SUDOERS_DIR/homelab-agent"
OLD_SUDOERS="$SUDOERS_DIR/homelab-agent-update"
# Прежняя раскладка (переносится ниже)
OLD_ETC=/etc/homelab-agent
OLD_LIB=/usr/local/lib/homelab-agent
OLD_STATE=/var/lib/homelab-agent
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DISPLAY_OPT=0
UPGRADE=0
VPN_OPT=0
OWN_PY=0
for arg in "$@"; do
    case "$arg" in
        --display) DISPLAY_OPT=1 ;;
        --vpn) VPN_OPT=1 ;;
        --own-python) OWN_PY=1 ;;
        --upgrade) UPGRADE=1 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "Неизвестный аргумент: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "ОШИБКА: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запустите через sudo"
[ -f "$SRC/homelab_agent/__main__.py" ] || die "не найден пакет homelab_agent рядом со скриптом ($SRC)"
# Python агента: свой (--own-python или уже поставленный раньше — в $ROOT/python) или системный python3
if [ "$OWN_PY" -eq 1 ]; then
    say "Свой Python (python-setup): отдельно от системного"
    install -d -m 0755 -o root -g root "$ROOT"
    "$SRC/packaging/python-setup" install "$ROOT" || die "свой Python не поставился (текст выше)"
fi
PY="$("$SRC/packaging/python-setup" path "$ROOT")"
PY="${PY:-python3}"
command -v "$PY" >/dev/null || die "нужен python3 (apt install python3 python3-venv) или --own-python"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "нужен Python 3.11+ (Raspberry Pi OS bookworm, Debian 12, Ubuntu 24.04)"
"$PY" -c 'import venv, ensurepip' 2>/dev/null || die "нужен модуль venv: apt install python3-venv (или --own-python)"

say "Пользователь $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
    # nologin лежит по-разному: /usr/sbin (Debian, Fedora), /usr/bin (Arch), /sbin (старые)
    NOLOGIN="$(command -v nologin || true)"
    useradd --system --no-create-home --home-dir /nonexistent --shell "${NOLOGIN:-/bin/false}" "$APP_USER"
fi
if [ "$DISPLAY_OPT" -eq 1 ] || { [ "$UPGRADE" -eq 1 ] && { [ -f "$APP/.display" ] || [ -f "$ROOT/.display" ]; }; }; then
    DISPLAY_OPT=1
    if getent group i2c >/dev/null; then
        usermod -a -G i2c "$APP_USER"
    else
        echo "  внимание: нет группы i2c — включите I2C: sudo raspi-config nonint do_i2c 0"
    fi
fi

VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$SRC/homelab_agent/__init__.py" | head -n 1)"
[[ "$VERSION" =~ ^[0-9][0-9A-Za-z.+_-]{0,31}$ ]] || die "не удалось определить версию агента (homelab_agent/__init__.py)"

say "Папка $ROOT"
# Родитель принадлежит root: кто владеет $ROOT, тот может подменить bin/ (его запускает root через sudo).
# Раньше каталог принадлежал агенту — приводим к root:root.
install -d -m 0755 -o root -g root "$ROOT" "$BIN" "$SYSD"
chown root:root "$ROOT"
chmod 0755 "$ROOT"
install -d -m 0755 -o "$APP_USER" -g "$APP_USER" "$APP"
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$STATE"
# etc: root владеет каталогом, группа агента пишет в него (panel.toml, vpn/); sticky-бит (1xxx) не даёт агенту
# удалить или переименовать чужие (root) файлы, например agent.toml
install -d -m 0750 -o root -g "$APP_USER" "$ETC"
chmod 1770 "$ETC"

# ---- Перенос прежней раскладки. Копируем/переносим и проверяем; удаляем старое только в конце, после успеха ----
# Копия содержимого каталога без перезаписи уже существующего (идемпотентно; cp -n ненадёжен по коду возврата)
copy_missing() {
    local from="$1" to="$2" entry name
    while IFS= read -r -d '' entry; do
        name="$(basename "$entry")"
        if [ ! -e "$to/$name" ] && [ ! -L "$to/$name" ]; then
            cp -a -- "$entry" "$to/$name"
        fi
    done < <(find "$from" -mindepth 1 -maxdepth 1 -print0)
}
# Всё ли из $1 есть в $2 (на верхнем уровне)
all_copied() {
    local from="$1" to="$2" entry
    while IFS= read -r -d '' entry; do
        [ -e "$to/$(basename "$entry")" ] || [ -L "$to/$(basename "$entry")" ] || return 1
    done < <(find "$from" -mindepth 1 -maxdepth 1 -print0)
}

MOVE_OLD_ETC=0
MOVE_OLD_STATE=0
if [ -d "$OLD_ETC" ]; then
    say "Перенос $OLD_ETC → $ETC"
    copy_missing "$OLD_ETC" "$ETC"
    if [ -f "$ETC/agent.toml" ] && grep -q "$OLD_ETC" "$ETC/agent.toml"; then
        # Пути токенов и CA в конфиге указывали на старый каталог; копия исходного файла остаётся рядом
        [ -e "$ETC/agent.toml.pre-migrate" ] || cp -p -- "$ETC/agent.toml" "$ETC/agent.toml.pre-migrate"
        sed -i "s|$OLD_ETC|$ETC|g" "$ETC/agent.toml"
    fi
    if all_copied "$OLD_ETC" "$ETC"; then MOVE_OLD_ETC=1; else echo "  внимание: перенос $OLD_ETC неполон — старое не удаляю"; fi
fi
if [ -d "$OLD_STATE" ]; then
    copy_missing "$OLD_STATE" "$STATE"
    if all_copied "$OLD_STATE" "$STATE"; then MOVE_OLD_STATE=1; fi
fi
# releases/current/previous лежали прямо в $ROOT, теперь в $APP. mv в пределах одной ФС мгновенный; относительные
# ссылки current → releases/<версия> остаются верными, пока переносятся вместе.
for n in releases current previous .display; do
    if { [ -e "$ROOT/$n" ] || [ -L "$ROOT/$n" ]; } && [ ! -e "$APP/$n" ] && [ ! -L "$APP/$n" ]; then
        echo "  перенос $ROOT/$n → $APP/$n"
        mv -- "$ROOT/$n" "$APP/$n"
    fi
done
install -d -m 0755 "$APP/releases"

# ---- Код ----
# Раскладка: $APP/releases/<версия>/{homelab_agent,.venv,requirements*.txt}, current → текущая, previous → прежняя.
# Юнит смотрит на current, поэтому обновление из панели ([update]) и сторож меняют версию без правки юнита.
say "Код в $APP/releases/$VERSION"
if [ -d "$ROOT/homelab_agent" ] && [ ! -L "$ROOT/homelab_agent" ] && [ ! -L "$APP/current" ]; then
    # Самая старая установка (код и .venv прямо в $ROOT): переносим в releases/<версия>
    OLDVER="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/homelab_agent/__init__.py" | head -n 1)"
    [[ "$OLDVER" =~ ^[0-9][0-9A-Za-z.+_-]{0,31}$ ]] || OLDVER="0.0.0-legacy"
    echo "  перенос прежней установки ($OLDVER) в releases/$OLDVER"
    install -d -m 0755 "$APP/releases/$OLDVER"
    mv -- "$ROOT/homelab_agent" "$APP/releases/$OLDVER/homelab_agent"
    [ ! -d "$ROOT/.venv" ] || mv -- "$ROOT/.venv" "$APP/releases/$OLDVER/.venv"
    for f in requirements.txt requirements-display.txt; do
        [ ! -f "$ROOT/$f" ] || cp -- "$ROOT/$f" "$APP/releases/$OLDVER/$f"
    done
    touch "$APP/releases/$OLDVER/.verified"
    ln -sfn "releases/$OLDVER" "$APP/current"
fi
REL="$APP/releases/$VERSION"
install -d -m 0755 "$REL"
# Пакет заменяется целиком, чтобы не оставались удалённые модули; venv и конфиг не трогаем
rm -r -f -- "$REL/homelab_agent.new"
cp -r -- "$SRC/homelab_agent" "$REL/homelab_agent.new"
find "$REL/homelab_agent.new" -name '__pycache__' -type d -prune -exec rm -r -f -- {} +
rm -r -f -- "$REL/homelab_agent"
mv -- "$REL/homelab_agent.new" "$REL/homelab_agent"
for f in requirements.txt requirements-display.txt; do
    install -m 0644 "$SRC/$f" "$REL/$f"
done

# bin/ — только root: скрипты, которые запускает root (sudo из агента, таймер сторожа), и справочные файлы.
# Сторож лежит вне app/ и не принадлежит агенту: он должен работать, даже если новая версия сломана.
say "bin/ (root)"
for f in README.md agent.example.toml agent.full.toml; do
    install -m 0644 -o root -g root "$SRC/$f" "$BIN/$f"
done
install -m 0644 -o root -g root "$SRC/packaging/sudoers.example" "$BIN/sudoers.example"
install -m 0644 -o root -g root "$SRC/homelab_agent/guard.py" "$BIN/guard.py"
install -m 0755 -o root -g root "$SRC/uninstall-agent" "$BIN/uninstall-agent"

say "Виртуальное окружение и зависимости"
# /tmp на Pi — tmpfs в памяти (сотни МБ): занят — ensurepip и pip падают с «No space left on device».
# Мало места — временные файлы на диск (/var/tmp); системный /tmp не трогаем
if [ "$(df -Pk /tmp 2>/dev/null | awk 'NR==2 {print $4}')" -lt 307200 ] 2>/dev/null; then
    echo "  в /tmp меньше 300 МБ свободно — временные файлы установки в /var/tmp"
    export TMPDIR=/var/tmp
fi
# venv на другом Python (переход на свой Python или обратно) — пересоздаём, иначе остался бы на прежнем
if [ -x "$REL/.venv/bin/python" ] && [ "$("$REL/.venv/bin/python" -c 'import sys; print(sys.base_prefix)' 2>/dev/null)" != "$("$PY" -c 'import sys; print(sys.base_prefix)')" ]; then
    rm -r -f -- "$REL/.venv"
fi
# Остаток прерванной попытки (venv есть, а pip нет — ensurepip упал) — тоже пересоздаём
if [ -x "$REL/.venv/bin/python" ] && ! "$REL/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "  venv без pip (прошлая установка прервалась) — пересоздаю"
    rm -r -f -- "$REL/.venv"
fi
if [ ! -x "$REL/.venv/bin/python" ]; then
    "$PY" -m venv "$REL/.venv" || { rm -r -f -- "$REL/.venv"; die "venv не создан (текст выше): проверьте место в /tmp и на диске (df -h / /tmp) и пакет python3-venv; или установите с --own-python"; }
fi
# python -m pip, а не bin/pip: у перенесённых venv в скриптах прежний абсолютный путь
"$REL/.venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
if [ "$DISPLAY_OPT" -eq 1 ]; then
    "$REL/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$REL/requirements-display.txt"
    touch "$APP/.display"
else
    "$REL/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$REL/requirements.txt"
fi
touch "$REL/.verified"

# Переключение на новую версию — атомарно (mv -T заменяет сам symlink, а не каталог за ним)
CUR=""
[ ! -L "$APP/current" ] || CUR="$(basename "$(readlink -f "$APP/current")")"
if [ -n "$CUR" ] && [ "$CUR" != "$VERSION" ] && [ -d "$APP/releases/$CUR" ]; then
    ln -sfn "releases/$CUR" "$APP/previous.new"
    mv -T -- "$APP/previous.new" "$APP/previous"
fi
ln -sfn "releases/$VERSION" "$APP/current.new"
mv -T -- "$APP/current.new" "$APP/current"
# app/ и релизы принадлежат агенту: [update] скачивает новые версии и переключает symlink от его имени
chown "$APP_USER:$APP_USER" "$APP"
chown -R "$APP_USER:$APP_USER" "$APP/releases"
chown -h "$APP_USER:$APP_USER" "$APP/current"
[ ! -L "$APP/previous" ] || chown -h "$APP_USER:$APP_USER" "$APP/previous"
[ ! -e "$APP/.display" ] || chown "$APP_USER:$APP_USER" "$APP/.display"
chown -R "$APP_USER:$APP_USER" "$STATE"

say "Конфиг в $ETC"
if [ ! -f "$ETC/agent.toml" ]; then
    # root владеет, агент читает (группа): сам себе правила действий он записать не может
    install -m 0640 -o root -g "$APP_USER" "$SRC/agent.example.toml" "$ETC/agent.toml"
    if [ "$DISPLAY_OPT" -eq 1 ]; then
        sed -i 's/^enabled = false /enabled = true  /' "$ETC/agent.toml"
    fi
    echo "  создан $ETC/agent.toml из примера — отредактируйте: sudoedit $ETC/agent.toml"
else
    echo "  $ETC/agent.toml уже есть — не трогаю"
    if [ "$UPGRADE" -eq 1 ] && ! grep -Eq '^[[:space:]]*\[update\]' "$ETC/agent.toml"; then
        # Обновление из панели включено по решению владельца; дописываем в конец, остальное не меняем
        printf '\n# Обновление агента из панели (дописано install.sh --upgrade)\n[update]\nenabled = true\n' >> "$ETC/agent.toml"
        echo "  дописан раздел [update] enabled = true"
    fi
fi
# Права на существующие файлы токенов приводим к 0600 (содержимое не читаем и не выводим)
for t in "$ETC"/token-*; do
    [ -e "$t" ] || continue
    chown "$APP_USER:$APP_USER" "$t"
    chmod 0600 "$t"
done
if [ -f "$ETC/panel.toml" ]; then
    chown "$APP_USER:$APP_USER" "$ETC/panel.toml"
    chmod 0600 "$ETC/panel.toml"
fi
if [ -d "$ETC/vpn" ]; then
    chown -R "$APP_USER:$APP_USER" "$ETC/vpn"
    chmod 0700 "$ETC/vpn"
fi

# Один файл sudoers для агента: перезапуск своей службы (обновление и настройки из панели) и удаление по команде
# панели. Правила действий и vpn, которые владелец уже добавил, сохраняются (наш блок между маркерами заменяется).
# Старый файл homelab-agent-update вливается и удаляется. Проверяется visudo — с ошибкой ничего не меняется.
SYSTEMCTL="$(command -v systemctl)"
# vpn-setup из агента — через systemd-run: отдельная служба вне песочницы агента (там /usr только для чтения)
SETUP_RUN="$(command -v systemd-run) --wait --pipe --collect --quiet"
if command -v visudo >/dev/null; then
    tmp_sudoers="$(mktemp)"
    if [ -f "$SUDOERS" ]; then
        sed '/^# BEGIN homelab-agent managed/,/^# END homelab-agent managed/d' "$SUDOERS" > "$tmp_sudoers"
    fi
    {
        echo '# BEGIN homelab-agent managed (install.sh перезаписывает этот блок; свои правила — вне него)'
        echo "$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart homelab-agent.service"
        echo "$APP_USER ALL=(root) NOPASSWD: $BIN/uninstall-agent --from-panel"
        if [ "$VPN_OPT" -eq 1 ]; then
            echo "$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart homelab-vpn.service"
            echo "$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop homelab-vpn.service"
            echo "$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart homelab-vpn-xray.service"
            echo "$APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop homelab-vpn-xray.service"
            echo "$APP_USER ALL=(root) NOPASSWD: $SETUP_RUN $BIN/vpn-setup install"
            echo "$APP_USER ALL=(root) NOPASSWD: $SETUP_RUN $BIN/vpn-setup install-xray"
        else
            echo '# Правила vpn и действий — по образцу в bin/sudoers.example, добавляйте вне этого блока:'
            echo "# $APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart homelab-vpn.service"
            echo "# $APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop homelab-vpn.service"
            echo "# $APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart homelab-vpn-xray.service"
            echo "# $APP_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop homelab-vpn-xray.service"
            echo "# $APP_USER ALL=(root) NOPASSWD: $SETUP_RUN $BIN/vpn-setup install"
            echo "# $APP_USER ALL=(root) NOPASSWD: $SETUP_RUN $BIN/vpn-setup install-xray"
        fi
        echo '# END homelab-agent managed'
    } >> "$tmp_sudoers"
    if [ -f "$OLD_SUDOERS" ]; then
        # Свои строки из старого файла (кроме комментариев), которых ещё нет в новом
        while IFS= read -r line; do
            case "$line" in ''|'#'*) continue ;; esac
            grep -qxF -- "$line" "$tmp_sudoers" || printf '%s\n' "$line" >> "$tmp_sudoers"
        done < "$OLD_SUDOERS"
    fi
    if visudo -cf "$tmp_sudoers" >/dev/null; then
        install -m 0440 -o root -g root "$tmp_sudoers" "$SUDOERS"
        rm -f -- "$OLD_SUDOERS"
        echo "  правила sudo: $SUDOERS"
    else
        echo "  внимание: правила sudo не прошли visudo — файлы sudoers не тронуты, обновление и удаление из панели недоступны"
    fi
    rm -f "$tmp_sudoers"
else
    echo "  внимание: нет visudo — правила sudo не поставлены, обновление и удаление из панели недоступны"
fi

# [vpn] enabled = true в agent.toml; повтор ничего не дублирует (раздел есть — правим/добавляем одну строку)
enable_vpn_in_config() {
    python3 - "$ETC/agent.toml" <<'PY'
import re, sys
path = sys.argv[1]
lines = open(path, encoding="utf-8").read().split("\n")
start = next((i for i, l in enumerate(lines) if re.match(r"\s*\[vpn\]\s*(#.*)?$", l)), None)
if start is None:
    lines += ["", "# VPN узла (дописано install.sh --vpn)", "[vpn]", "enabled = true"]
else:
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    for i in range(start + 1, end):
        if re.match(r"\s*enabled\s*=", lines[i]):
            lines[i] = "enabled = true"
            break
    else:
        lines.insert(start + 1, "enabled = true")
open(path, "w", encoding="utf-8").write("\n".join(lines))
PY
    echo "  в $ETC/agent.toml включено [vpn] enabled = true"
}

say "Службы systemd"
# Юниты лежат в $SYSD (root), в $UNIT_DIR — ссылки (то же, что делает `systemctl link`, но замена атомарная,
# в том числе поверх обычного файла прежней установки). Удаление папки + ссылок убирает всё.
install -m 0644 -o root -g root "$SRC/packaging/homelab-agent.service" "$SRC/packaging/homelab-agent-guard.service" \
    "$SRC/packaging/homelab-agent-guard.timer" "$SYSD/"
if [ "$VPN_OPT" -eq 1 ]; then
    # VPN узла: каталог конфига — только агенту; служба sing-box ставится, но не включается (включит панель)
    install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$ETC/vpn"
    install -m 0644 -o root -g root "$SRC/packaging/homelab-vpn.service" "$SRC/packaging/homelab-vpn-xray.service" "$SYSD/"
    # Системный пользователь движка Xray: без входа и без дома, ничего не владеет
    if ! id hl-xray >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir /nonexistent --shell "$(command -v nologin || echo /bin/false)" hl-xray
    fi
    install -m 0755 -o root -g root "$SRC/packaging/vpn-setup" "$BIN/vpn-setup"
    enable_vpn_in_config
elif [ -e "$UNIT_DIR/homelab-vpn.service" ]; then
    install -m 0644 -o root -g root "$SRC/packaging/homelab-vpn.service" "$SRC/packaging/homelab-vpn-xray.service" "$SYSD/"
fi
link_unit() {
    local name="$1"
    [ -f "$SYSD/$name" ] || return 0
    [ "$(readlink -f "$UNIT_DIR/$name" 2>/dev/null || true)" = "$SYSD/$name" ] && return 0
    ln -sfn "$SYSD/$name" "$UNIT_DIR/$name.new"
    mv -T -- "$UNIT_DIR/$name.new" "$UNIT_DIR/$name"
}
for u in homelab-agent.service homelab-agent-guard.service homelab-agent-guard.timer homelab-vpn.service homelab-vpn-xray.service; do
    link_unit "$u"
done
systemctl daemon-reload
systemctl enable --now homelab-agent-guard.timer >/dev/null 2>&1 || echo "  внимание: таймер сторожа не включился"
if [ "$VPN_OPT" -eq 1 ]; then
    # Сбой сети — не ошибка установки: агент повторит vpn-setup install при первом конфиге от панели
    say "sing-box (vpn-setup install)"
    "$BIN/vpn-setup" install || echo "  внимание: sing-box не поставился (см. текст выше) — агент попробует снова, когда панель пришлёт VPN"
    say "Xray (vpn-setup install-xray)"
    "$BIN/vpn-setup" install-xray || echo "  внимание: Xray не поставился (см. текст выше) — агент попробует снова, если sing-box не подойдёт"
fi

# ---- Старая раскладка больше не нужна: удаляем только то, что перенесено и проверено ----
if [ "$MOVE_OLD_ETC" -eq 1 ]; then rm -r -f -- "$OLD_ETC"; fi
if [ "$MOVE_OLD_STATE" -eq 1 ]; then rm -r -f -- "$OLD_STATE"; fi
rm -r -f -- "$OLD_LIB"
for f in requirements.txt requirements-display.txt agent.example.toml agent.full.toml README.md; do
    rm -f -- "${ROOT:?}/$f"
done
rm -r -f -- "${ROOT:?}/packaging"
# временные ссылки самой ранней раскладки
for l in homelab_agent .venv; do
    [ ! -L "$ROOT/$l" ] || rm -f -- "$ROOT/$l"
done

if [ "$UPGRADE" -eq 1 ]; then
    if systemctl is-enabled --quiet homelab-agent.service; then
        systemctl restart homelab-agent.service
        say "Обновлено и перезапущено. Журнал: journalctl -u homelab-agent -n 50"
    else
        say "Обновлено. Служба не включена: sudo systemctl enable --now homelab-agent"
    fi
    exit 0
fi

# start-agent сам делает следующие шаги — подсказки не нужны
[ -n "${HOMELAB_AGENT_QUIET:-}" ] && exit 0

cat <<EOF

Готово. Дальше (токены не попадают на экран и в историю команд):
  1) Токен узла из панели (Инфраструктура → Узлы → добавить узел), для каждой [[panel]] свой файл:
       sudo sh -c 'umask 077; cat > $ETC/.token-main.new'
     вставьте токен (Ctrl+Shift+V), Enter, затем Ctrl-D; потом:
       sudo chown $APP_USER: $ETC/.token-main.new && sudo mv -f $ETC/.token-main.new $ETC/token-main
  2) Конфиг: sudoedit $ETC/agent.toml   (node — как имя узла в панели, url панели)
     Все остальные настройки с примерами — $BIN/agent.full.toml
  3) Права на действия (если есть [actions.*]): sudo visudo -f $SUDOERS
     (добавляйте строки вне блока «managed»; образец — $BIN/sudoers.example)
  4) Проверка: cd $APP/current && sudo -u $APP_USER .venv/bin/python -m homelab_agent -c $ETC/agent.toml --check-config
     Пробный сбор: cd $APP/current && sudo -u $APP_USER .venv/bin/python -m homelab_agent -c $ETC/agent.toml --once
  5) Запуск:   sudo systemctl enable --now homelab-agent
     Журнал:   journalctl -u homelab-agent -f
EOF
