#!/usr/bin/env bash
# Запускается службой homelab-updater от root, когда панель положила <DATA_DIR>/update/request.json.
# Читает ТОЛЬКО поле version (проверка регэкспом), остальное из файла в команды не попадает.
# Ход пишет в update/log.txt, статус — в update/status.json (running → done | failed), затем вызывает deploy.sh.
#
#   tools/update-from-panel.sh <DATA_DIR>      # DATA_DIR подставляет юнит (install-updater.sh)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${1:?нужен путь к DATA_DIR}"
UPD="$DATA_DIR/update"
REQ="$UPD/request.json"
TAKEN="$UPD/request.taken"
STATUS="$UPD/status.json"
LOG="$UPD/log.txt"
HIST="$UPD/history.jsonl"

# Повторный запуск от PathChanged после удаления запроса, либо запрос уже отменён панелью
[ -f "$REQ" ] || exit 0

# Забираем запрос: пока он лежит в request.json, панель считает, что служба его ещё не взяла (можно отменить)
mv -f "$REQ" "$TAKEN" || exit 0

# Версия: строго X.Y.Z (с необязательным v) или latest. Берём только значение ключа "version"
VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$TAKEN" | head -n 1)"
rm -f "$TAKEN"
STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START_EPOCH="$(date +%s)"

write_status() { # state step [error]
  local tmp="$STATUS.tmp.$$"
  printf '{"state":"%s","step":"%s","version":"%s","started":"%s","ts":"%s","error":"%s"}\n' \
    "$1" "$2" "${VERSION//[^A-Za-z0-9.]/}" "$STARTED" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${3:-}" > "$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$STATUS"
}

: > "$LOG"
chmod 644 "$LOG"

if ! [[ "$VERSION" =~ ^(v?[0-9]+\.[0-9]+\.[0-9]+|latest)$ ]]; then
  echo "!! Неверная версия в запросе — обновление отклонено" >> "$LOG"
  VERSION=""
  write_status failed request "неверная версия в запросе"
  exit 1
fi

write_status running start
echo "== Обновление панели до $VERSION ($STARTED)" >> "$LOG"

# deploy.sh сам обновляет status.json по шагам (бэкап, загрузка, запуск, проверка)
export UPDATE_STATUS_FILE="$STATUS" UPDATE_STARTED="$STARTED" UPDATE_VERSION="${VERSION//[^A-Za-z0-9.]/}"
"$ROOT/tools/deploy.sh" pull "$VERSION" >> "$LOG" 2>&1
CODE=$?

SECONDS_TOTAL=$(( $(date +%s) - START_EPOCH ))
if [ "$CODE" -eq 0 ]; then
  write_status done finish
  RESULT=done
else
  # Шаг, на котором остановились, записал deploy.sh (state=running, step=…)
  LAST_STEP="$(sed -n 's/.*"step":"\([A-Za-z_]*\)".*/\1/p' "$STATUS" | head -n 1)"
  write_status failed "${LAST_STEP:-deploy}" "deploy.sh завершился с кодом $CODE; образ и БД возвращены, подробности в журнале"
  RESULT=failed
fi

# История длительностей для ETA панели: последние 10 строк
printf '{"version":"%s","seconds":%s,"state":"%s"}\n' "${VERSION//[^A-Za-z0-9.]/}" "$SECONDS_TOTAL" "$RESULT" >> "$HIST"
tail -n 10 "$HIST" > "$HIST.tmp" && mv -f "$HIST.tmp" "$HIST"
chmod 644 "$HIST"
exit "$CODE"
