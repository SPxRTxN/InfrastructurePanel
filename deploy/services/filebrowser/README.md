# File Browser — заготовка

Файлы homelab в браузере, оформление как у панели (графит, индиго, Onest). Работает **двумя способами**:
- **сам по себе** — `http://<сервер>:8081`, свой вход (логин и пароль File Browser), панель для этого не нужна;
- **из панели** — раздел «Сервисы» → File Browser → «Открыть» (новая вкладка, тот же адрес).

Ставим **вместе с владельцем**: шаги ниже — команды владельцу на сервере (агенту Docker и sudo запрещены).

## Важно: диски с важными данными
- По умолчанию **не подключаются**: в `compose.yaml` их нет, `FB_FILES` указывает на отдельный новый каталог.
- Показать такой диск в File Browser — только строкой с `:ro` (только чтение) в `compose.yaml`, после проверки точки
  монтирования (`findmnt`). Ни форматирования, ни chown/chmod, ни записи на этот диск.

## Что в каталоге
- `compose.yaml` — образ `filebrowser/filebrowser:v2.63.23` (закреплён digest), порт 8081, корень контейнера только на чтение, без capabilities.
- `.env.example` → скопировать в `.env`: каталог файлов, порт, адрес, UID/GID.
- `branding/` — `custom.css`, логотип `img/logo.svg`, шрифт Onest (`img/fonts/`, OFL) — подключается настройкой File Browser.
- `data/` — появится при установке: база пользователей и настроек (`database/`), `config/settings.json`. В git не попадает.

## Установка (по шагам, вместе)
Каталог на сервере: `/opt/homelab-dashboard/deploy/services/filebrowser`. Команды — по одной.

1. Каталоги и права (UID/GID — тот, от кого будет работать контейнер; узнать: `id <пользователь>`):
   ```bash
   sudo install -d -o 1000 -g 1000 /mnt/homelab-data/filebrowser/files
   sudo install -d -o 1000 -g 1000 /opt/homelab-dashboard/deploy/services/filebrowser/data/database /opt/homelab-dashboard/deploy/services/filebrowser/data/config
   sudo cp /opt/homelab-dashboard/deploy/services/filebrowser/.env.example /opt/homelab-dashboard/deploy/services/filebrowser/.env
   ```
   Проверить `.env` (`FB_FILES`, `FB_UID`, `FB_GID`).
2. Первый запуск — File Browser сам создаст базу и **случайный пароль** администратора `admin`:
   ```bash
   sudo bash -c 'cd /opt/homelab-dashboard/deploy/services/filebrowser && docker compose up -d'
   sudo bash -c 'cd /opt/homelab-dashboard/deploy/services/filebrowser && docker compose logs filebrowser | grep -i password'
   ```
   Пароль никому не пересылать; сразу сменить в File Browser: «Настройки → Профиль».
3. Оформление и язык (база занята, пока контейнер работает, — поэтому stop → run → up):
   ```bash
   sudo bash -c 'cd /opt/homelab-dashboard/deploy/services/filebrowser && docker compose stop'
   sudo bash -c 'cd /opt/homelab-dashboard/deploy/services/filebrowser && docker compose run --rm --entrypoint filebrowser filebrowser config set -d /database/filebrowser.db --branding.name "Homelab · Файлы" --branding.theme dark --branding.files /branding --branding.disableExternal --locale ru'
   sudo bash -c 'cd /opt/homelab-dashboard/deploy/services/filebrowser && docker compose up -d'
   ```
4. Проверка: `http://<сервер>:8081` — вход, тёмная тема в стиле панели, видны только файлы из `FB_FILES`.
5. В панели: «Сервисы» → «Добавить»: узел — сервер панели, вид — File Browser, проверка `http`,
   адрес проверки `http://127.0.0.1:8081/health`, веб-адрес `http://<сервер>:8081`.

## Доступ из панели — как сделано и что можно позже
Сейчас — ссылка «Открыть» (как веб-BMC в настройках узла): у File Browser свой вход. Встроить его внутрь страницы
панели или входить «одним входом» без пароля можно только через общий HTTPS-прокси с проверкой сессии панели
(режим `auth.method=proxy` у File Browser) — это отдельное решение, обсудим при подключении
([[Решения]]: «Веб-BMC — только ссылка, не прокси через панель»).

## Обслуживание
- Бэкап — каталог `data/database` (пользователи и настройки); сами файлы — в `FB_FILES`.
- Обновление: сменить тег и digest в `compose.yaml` (`docker buildx imagetools inspect filebrowser/filebrowser:<тег>`), затем `docker compose pull` и `up -d`.
- Остановить: `docker compose down` (данные остаются в `data/` и `FB_FILES`).
