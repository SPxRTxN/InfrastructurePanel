#!/usr/bin/env bash
# Ставит службу обновления панели (systemd path + service). Запускать один раз от владельца через sudo:
#
#   sudo tools/install-updater.sh
#
# Пути берутся из .env рядом с compose.yaml: DATA_DIR (по умолчанию ./data) и каталог проекта.
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo: sudo tools/install-updater.sh"; exit 1; }
[ -f .env ] || { echo "Нет .env рядом с compose.yaml"; exit 1; }

PROJECT_DIR="$(pwd)"
DATA_DIR="$(sed -n 's/^DATA_DIR=//p' .env | tail -n 1)"
DATA_DIR="${DATA_DIR:-data}"
case "$DATA_DIR" in /*) ;; *) DATA_DIR="$PROJECT_DIR/${DATA_DIR#./}" ;; esac
# Пути попадают в unit-файл: допускаем только безопасные символы
for p in "$PROJECT_DIR" "$DATA_DIR"; do
  [[ "$p" =~ ^/[A-Za-z0-9._/-]+$ ]] || { echo "Недопустимые символы в пути: $p"; exit 1; }
done

# Каталог обмена принадлежит пользователю контейнера: панель пишет туда запрос, служба (root) — статус и лог
OWNER="$(stat -c '%u:%g' "$PROJECT_DIR/compose.yaml")"
mkdir -p "$DATA_DIR/update"
chown "$OWNER" "$DATA_DIR/update"
chmod 700 "$DATA_DIR/update"
chmod 755 tools/update-from-panel.sh tools/deploy.sh

for unit in homelab-updater.path homelab-updater.service; do
  sed -e "s|@DATA_DIR@|$DATA_DIR|g" -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" "tools/$unit" > "/etc/systemd/system/$unit"
  chmod 644 "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now homelab-updater.path
# Отметка для панели («Настройки → Обновление»): из контейнера systemd хоста не виден
printf '{"installed_at": "%s"}\n' "$(date '+%Y-%m-%d %H:%M')" > "$DATA_DIR/update/updater.json"
chown "$OWNER" "$DATA_DIR/update/updater.json"
chmod 644 "$DATA_DIR/update/updater.json"
echo "Готово: homelab-updater.path включён, следит за $DATA_DIR/update/request.json"
