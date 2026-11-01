#!/usr/bin/env bash
# Выкладка на сервере: бэкап БД → новый образ → запуск → ожидание healthy → откат при ошибке.
#
#   tools/deploy.sh            # образ :latest из GHCR
#   tools/deploy.sh 1.2.0      # конкретная версия (git tag v1.2.0) или sha-abc1234
#   tools/deploy.sh build      # собрать образ из исходников на этом устройстве (без реестра)
#
# Запускать из клона репозитория, где рядом с compose.yaml лежит .env.
# При откате восстанавливается бэкап, снятый перед выкладкой: новые миграции могли изменить схему,
# а старый код с новой схемой работать не обязан. Данные за минуты выкладки при откате теряются.
#   tools/deploy.sh pull 1.2.0 # то же, но из реестра явно (так вызывает служба обновления панели, update-from-panel.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="${1:-latest}"
IMAGE="${DASHBOARD_IMAGE:-ghcr.io/spxrtxn/infrastructurepanel}"

# Режим pull <тег>: образ из реестра $IMAGE:<тег>, никогда не локальная сборка. Тег — как у git-тега без «v».
if [ "$TARGET" = pull ]; then
  TARGET="${2:-latest}"
  TARGET="${TARGET#v}"
  [[ "$TARGET" =~ ^([0-9]+\.[0-9]+\.[0-9]+|latest)$ ]] || { echo "Неверный тег версии: $TARGET"; exit 1; }
fi

# Ход выкладки для панели: если задан UPDATE_STATUS_FILE (его задаёт update-from-panel.sh), каждый шаг
# записывается в status.json. Без переменной — ничего не делает, поведение остальных режимов прежнее.
step() {
  [ -n "${UPDATE_STATUS_FILE:-}" ] || return 0
  local tmp="$UPDATE_STATUS_FILE.tmp.$$"
  printf '{"state":"running","step":"%s","version":"%s","started":"%s","ts":"%s","error":""}\n' \
    "$1" "${UPDATE_VERSION:-}" "${UPDATE_STARTED:-}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" || return 0
  chmod 644 "$tmp"
  mv -f "$tmp" "$UPDATE_STATUS_FILE" || true
}

[ -f .env ] || { echo "Нет .env — скопируйте .env.example и заполните SECRET_KEY"; exit 1; }
[ -r .env ] || { echo "Нет прав на чтение .env — запустите от владельца файлов или через sudo"; exit 1; }
env_value() { sed -n "s/^$1=//p" .env | tail -n 1; }
# Путь проекта на хосте для подсказок панели (compose.yaml → PROJECT_DIR); не задан в .env — этот клон
PROJECT_DIR="$(env_value PROJECT_DIR)"
export PROJECT_DIR="${PROJECT_DIR:-$PWD}"

# Пользователь контейнера = владелец данных и ключей. Порядок: окружение → .env → тот, кто запускает.
# Через sudo id -u = 0, поэтому без DASHBOARD_UID в .env контейнер не запускаем (не от root).
DASHBOARD_UID="${DASHBOARD_UID:-$(env_value DASHBOARD_UID)}"
DASHBOARD_GID="${DASHBOARD_GID:-$(env_value DASHBOARD_GID)}"
export DASHBOARD_UID="${DASHBOARD_UID:-$(id -u)}" DASHBOARD_GID="${DASHBOARD_GID:-$(id -g)}"
[ "$DASHBOARD_UID" != 0 ] || { echo "Контейнер не запускается от root: задайте DASHBOARD_UID/DASHBOARD_GID в .env"; exit 1; }

# БД и бэкапы могут лежать на других дисках (DATA_DIR, BACKUP_DIR в .env) — те же пути, что монтирует compose.yaml
DATA_DIR="$(env_value DATA_DIR)"
DATA_DIR="${DATA_DIR:-data}"
BACKUP_DIR="$(env_value BACKUP_DIR)"
BACKUP_DIR="${BACKUP_DIR:-data/backups}"
mkdir -p "$DATA_DIR" ssh "$BACKUP_DIR"
if [ "$(id -u)" = 0 ]; then
  # Под sudo созданные каталоги принадлежали бы root — отдаём их пользователю контейнера
  chown "$DASHBOARD_UID:$DASHBOARD_GID" "$DATA_DIR" ssh "$BACKUP_DIR"
fi
chmod 700 "$DATA_DIR" "$BACKUP_DIR"

container_id() { docker compose ps -q dashboard 2>/dev/null || true; }

wait_healthy() {
  local id status
  for _ in $(seq 1 30); do
    id="$(container_id)"
    status="$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || echo none)"
    [ "$status" = healthy ] && return 0
    [ "$status" = unhealthy ] && return 1
    sleep 3
  done
  return 1
}

# 1. Бэкап и запоминание текущего образа для отката
OLD_ID="$(container_id)"
BACKUP=""
step backup
if [ -n "$OLD_ID" ]; then
  echo "== Бэкап БД"
  # --no-age: для отката копию нужно восстановить здесь же, без приватного ключа age
  docker compose exec -T dashboard python -m app.cli backup --no-age
  # Бэкап пишет ещё работающий старый контейнер — в каталог, примонтированный ему. Если BACKUP_DIR в .env
  # только что сменили, это прежний каталог, а не новый: берём путь из монтирования контейнера.
  OLD_BACKUP_DIR="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data/backups"}}{{.Source}}{{end}}{{end}}' "$OLD_ID")"
  OLD_BACKUP_DIR="${OLD_BACKUP_DIR:-$BACKUP_DIR}"
  BACKUP="$(ls -1t "$OLD_BACKUP_DIR"/homelab-*.db 2>/dev/null | head -n 1 || true)"
  [ -n "$BACKUP" ] || { echo "!! Не найден бэкап в $OLD_BACKUP_DIR — выкладка остановлена, ничего не изменено"; exit 1; }
  if [ "$OLD_BACKUP_DIR" != "$(readlink -f "$BACKUP_DIR")" ] && [ "$OLD_BACKUP_DIR" != "$BACKUP_DIR" ]; then
    echo "   (бэкап перед выкладкой — в прежнем каталоге $OLD_BACKUP_DIR; новые бэкапы пойдут в $BACKUP_DIR)"
  fi
  docker tag "$(docker inspect -f '{{.Image}}' "$OLD_ID")" "$IMAGE:rollback"
fi

# 2. Новый образ
if [ "$TARGET" = build ]; then
  export DASHBOARD_TAG=local
  echo "== Сборка образа из исходников"
  # Сборка в сети хоста: pip берёт DNS сервера — в bridge-сети Docker сеть/DNS может не работать
  # (локальный резолвер 127.x, VPN/TUN на хосте). docker build, а не compose build: новый compose
  # собирает через bake, которому network=host нужно разрешать отдельно
  docker build --network host -t "$IMAGE:$DASHBOARD_TAG" .
else
  export DASHBOARD_TAG="$TARGET"
  step pull
  echo "== Загрузка $IMAGE:$DASHBOARD_TAG"
  docker compose pull dashboard
fi

# 3. Запуск и проверка
step up
docker compose up -d dashboard
step healthy
if wait_healthy; then
  echo "== Готово: $IMAGE:$DASHBOARD_TAG работает"
  docker image prune -f >/dev/null
  # Служба обновления из панели: ставится/обновляется сама при ручной выкладке (под sudo), чтобы дальше всё
  # обновлялось из веб-панели. Из самой службы (UPDATE_STATUS_FILE задан) не переустанавливаем — она сейчас работает.
  if [ "$(id -u)" = 0 ] && [ -z "${UPDATE_STATUS_FILE:-}" ] && [ -f tools/install-updater.sh ]; then
    bash tools/install-updater.sh || echo "!! Служба обновления из панели не установилась — обновление из панели недоступно"
  fi
  exit 0
fi

echo "!! Новая версия не прошла healthcheck. Логи:"
docker compose logs --tail 50 dashboard || true
if [ -z "$OLD_ID" ]; then
  echo "!! Предыдущей версии нет — откатывать некуда"
  exit 1
fi

# 4. Откат: старый образ + бэкап БД
step rollback
echo "== Откат на предыдущий образ и бэкап $BACKUP"
docker compose stop dashboard
DB="$DATA_DIR/homelab.db"
cp "$DB" "$DB.failed-$(date +%Y%m%d-%H%M%S)"
rm -f "$DB-wal" "$DB-shm"
cp "$BACKUP" "$DB"
chmod 600 "$DB"
export DASHBOARD_TAG=rollback
docker compose up -d dashboard
if wait_healthy; then
  echo "== Откат выполнен, работает предыдущая версия"
else
  echo "!! Откат тоже не поднялся — нужна ручная проверка: docker compose logs dashboard"
fi
exit 1
