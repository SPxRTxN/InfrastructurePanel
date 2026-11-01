# Homelab Dashboard

Веб-дашборд домашней лаборатории: мониторинг серверов, канбан (Jira), база знаний (Confluence), календарь с напоминаниями и схема сети. Тёмная тема по умолчанию, права на основе групп.

**Стек:** Python 3.12, FastAPI, Jinja2 + HTMX, SQLite (SQLAlchemy 2 + Alembic), SortableJS, FullCalendar, Chart.js, Cytoscape.js, иконки Lucide. Все JS-библиотеки лежат локально в `app/static/vendor/`, CDN в работе не используется.

## Возможности

| Раздел | Что умеет |
|---|---|
| Главная | KPI-карточки, статус серверов (RAM-кольца, диски), задачи по статусам, ближайшие события, последние статьи. Набор и порядок виджетов — в настройках |
| Инфраструктура → Серверы | Проверка по расписанию: SSH (TCP + баннер), RAM и разделы дисков (с агента, локально через psutil или по SSH одной read-only командой), графики 24 ч, история 14 дней со скачиванием, сервисы tcp/http |
| Инфраструктура → Узлы | Агенты [homelab-agent](handoff/homelab-agent/README.md) по WebSocket: температуры, нагрузка, RAM, диски, процессы, службы, свои проверки; действия восстановления из allowlist узла; токен на узел. Установка на узел: `tools/push-agent.sh` → `./start-agent` (Linux) / `start-agent.ps1` (Windows) |
| Инфраструктура → Карта сети | Топология: устройства и связи с портами, перетаскивание с сохранением, цвет иконки по статусу сервера; сервер ставится на карту при создании, устройство превращается в сервер |
| Инфраструктура → Оповещения | Правила (офлайн, диск, температура, датчик агента), «держится N мин», тишина, получатели по группам, колокольчик / Telegram / ntfy |
| Инфраструктура → VPN | Подписка (vless/reality, vmess, trojan, ss, hysteria2) → сервер → весь трафик узла через sing-box со сторожем отката (включается только на узле с `[vpn]`) |
| Инфраструктура → Архив | Удаление сервера/узла/устройства сервера из любого места убирает всё связанное; снимок с историей хранится 14 дней, скачивается JSON |
| Jira | Доски, настраиваемые колонки, задачи `HL-1` с Markdown-описанием, приоритетом, исполнителем, сроком, метками; drag-and-drop, поиск и фильтры; связанные статьи и «Создать артефакт» |
| Confluence | Пространства, дерево страниц, Markdown-редактор с предпросмотром, история версий с восстановлением, поиск, связанные задачи |
| Календарь | События с повтором (день/неделя/месяц) и напоминаниями → колокольчик и браузерные уведомления; сроки задач Jira |
| Настройки | Профиль и пароль, пользователи, группы и права, серверы и сервисы, внешний вид, виджеты главной, календарь, **Разделы** (выключить ненужные — для панели на Pi / узле администрирования), **Безопасность** (журнал входов и событий, сессии, CSV, 90 дней), **Бэкапы** (по расписанию, проверка восстановления), **Перенос** (экспорт/импорт конфигурации) |
| Руководство | Полное руководство — в самой панели: Confluence → пространство «Руководство» (обновляется вместе с версией) |

Установка без Docker (Raspberry Pi, отдельный узел администрирования) — [deploy/native/README.md](deploy/native/README.md).

Как пользоваться панелью — [docs/Руководство пользователя.md](docs/Руководство%20пользователя.md).

## Настройка перед первым запуском

Все настройки вашей установки — пути, адреса, порты, ключи — в одном файле **`.env`** в корне проекта.
Он в `.gitignore` и никогда не попадает в git; в репозитории лежит только образец `.env.example` без значений.
Код, тесты и примеры содержат лишь общие значения (`192.168.1.x`, `/opt/homelab-dashboard`), свои пишите только в `.env`.

1. Склонировать стабильную ветку:
   `git clone -b Dashboard-Main https://github.com/SPxRTxN/InfrastructurePanel.git /opt/homelab-dashboard && cd /opt/homelab-dashboard`
2. Создать файл настроек: `cp .env.example .env && chmod 600 .env`.
3. Заполнить в `.env`:
   - **`SECRET_KEY`** — обязательно (команда генерации — ниже и в самом файле);
   - **пути**: `PROJECT_DIR` (куда склонировали), `DATA_DIR` (БД, для Docker), `BACKUP_DIR` (бэкапы, лучше другой диск),
     `DATABASE_URL` (БД без Docker);
   - **сеть**: `HOST`/`PORT` (без Docker) или `DASHBOARD_BIND`/`DASHBOARD_PORT` (Docker);
   - по желанию — `FIRST_ADMIN_PASSWORD`, шифрование (`DB_ENCRYPTION_KEY`, `PASSWORD_PEPPER`, `BACKUP_AGE_RECIPIENT`),
     `GITHUB_REPO` (откуда брать релизы, если у вас форк), `DASHBOARD_IMAGE` (свой реестр образа).
4. Собрать и запустить одним из способов: **Docker** (раздел «Docker») или **без Docker** («Установка» → «Запуск»;
   Raspberry Pi и отдельный узел — [deploy/native/README.md](deploy/native/README.md)).
5. Войти `admin` / `admin` и сразу задать свои логин и пароль («Первый администратор»).

Обновления — готовые релизы из GitHub (`tools/deploy.sh` или «Настройки → Обновление»); `.env` и данные при этом
не меняются. Свои изменения в проект — pull request в ветку `dev`, порядок — [CONTRIBUTING.md](CONTRIBUTING.md).

## Установка

Нужен Python 3.12.

```bash
git clone <репозиторий> homelab-dashboard && cd homelab-dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Сгенерируйте ключ шифрования и впишите его в `SECRET_KEY` в `.env`:

```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Переменные `.env`:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `DATABASE_URL` | `<проект>/data/homelab.db` | Путь к БД (только абсолютный: `sqlite:////путь/homelab.db`) |
| `SECRET_KEY` | — | Ключ Fernet для хранимых секретов (путь к SSH-ключу). Обязателен — без него `serve` не стартует. **Не меняйте после настройки серверов** — иначе пути придётся ввести заново |
| `TZ` | пояс системы | Часовой пояс календаря и напоминаний, например `Europe/Moscow` |
| `FIRST_ADMIN_PASSWORD` | `admin` | Пароль `admin` при первом запуске на пустой БД |
| `DB_ENCRYPTION_KEY` | — | 64 hex: шифрование всей БД SQLCipher |
| `PASSWORD_PEPPER` | — | «Перец» паролей (HMAC перед argon2id) |
| `BACKUP_AGE_RECIPIENT` | — | Публичный ключ age для шифрования бэкапов |
| `DATA_DIR` | `./data` | Только Docker: каталог на хосте, который монтируется в `/data` (БД) |
| `BACKUP_DIR` | `<каталог БД>/backups` | Каталог бэкапов (лучше другой диск); в Docker монтируется в `/data/backups` |
| `<ИМЯ>_FILE` | — | Читать секрет из файла: `SECRET_KEY_FILE`, `DB_ENCRYPTION_KEY_FILE`, `PASSWORD_PEPPER_FILE`, `FIRST_ADMIN_PASSWORD_FILE` |
| `COOKIE_SECURE` | `false` | `true` за HTTPS-прокси: cookie сессии только по HTTPS |
| `SESSION_HOURS` | `12` | Время жизни сессии |
| `SCHEDULER_ENABLED` | `true` | Фоновые проверки серверов и напоминания |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Адрес для `app.cli serve`; `0.0.0.0` — доступ из локальной сети |
| `PROJECT_DIR` | каталог проекта (в Docker — из `tools/deploy.sh`) | Путь к проекту на хосте для подсказок с командами (`push-agent`) |
| `GITHUB_REPO` | репозиторий проекта | Откуда «Настройки → Обновление» берёт список релизов (`владелец/имя`) |
| `DASHBOARD_IMAGE` / `DASHBOARD_BIND` / `DASHBOARD_PORT` | образ GHCR / `127.0.0.1` / `8000` | Только Docker: образ и адрес публикации порта |

## Первый администратор

При **первом** запуске на пустой БД создаётся администратор **`admin` / `admin`**. После входа под ним открывается только страница «Первый вход»: пока не заданы новый логин (не `admin`) и пароль (от 8 символов), панель, настройки и API недоступны.

Учётная запись по умолчанию создаётся, только если в БД нет ни одного пользователя. Перезапуск, обновление образа, пересоздание контейнера её не возвращают и ничего не сбрасывают — пользователи, права, задачи, статьи и остальные данные остаются в БД.

> Пока логин и пароль не сменены, войти под `admin`/`admin` может любой, кто видит порт. Войдите сразу после первого запуска. Для сервера, доступного из интернета, задайте `FIRST_ADMIN_PASSWORD` — см. «Внешний сервер и защита данных».

Резервный способ — создать администратора из консоли (например, если потерян пароль единственного администратора):

```bash
.venv/bin/python -m app.cli create-admin                            # без Docker
docker compose exec dashboard python -m app.cli create-admin        # в Docker
```

Остальных пользователей администратор создаёт в **Настройки → Пользователи**.

## Начальные данные

```bash
.venv/bin/python -m app.cli seed
```

Добавляет серверы «Сервер (homelab)» (local) и «Raspberry Pi» (ssh, выключен) и схему сети из 11 устройств и 10 связей. Повторный запуск ничего не дублирует.

## Запуск

```bash
.venv/bin/python -m app.cli serve
```

Адрес и порт берутся из `HOST`/`PORT` в `.env` (по умолчанию `127.0.0.1:8000` — только этот ПК), можно переопределить: `serve --host 0.0.0.0 --port 8080`. Откройте http://127.0.0.1:8000.

**Доступ с других ПК локальной сети:** в `.env` укажите `HOST=0.0.0.0`, перезапустите и откройте с другого устройства `http://<IP этого ПК>:8000` (IP: `ip -4 addr` или `hostname -I`). Если не открывается — разрешите порт в брандмауэре, например `sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp`. Трафик по HTTP не шифруется — для доступа извне локальной сети нужен HTTPS-прокси (nginx, Caddy). За прокси:

```bash
# в .env: COOKIE_SECURE=true
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
```

`--proxy-headers` нужен, чтобы блокировка перебора паролей видела настоящий IP клиента, а не адрес прокси.

Пример unit-файла systemd (`/etc/systemd/system/homelab-dashboard.service`):

```ini
[Unit]
Description=Homelab Dashboard
After=network.target

[Service]
User=homelab
WorkingDirectory=/opt/homelab-dashboard
ExecStart=/opt/homelab-dashboard/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Docker

Один образ для `linux/amd64` (мини-ПК) и `linux/arm64` (Raspberry Pi). Нужны Docker и плагин Compose.

```bash
git clone https://github.com/SPxRTxN/InfrastructurePanel.git homelab && cd homelab
git checkout Dashboard-Main
cp .env.example .env && chmod 600 .env        # заполните — «Настройка перед первым запуском»
mkdir -p data ssh
tools/deploy.sh build                          # собрать образ здесь, без реестра
# или готовый образ из GHCR (если пакет закрытый — сначала docker login ghcr.io с токеном read:packages):
tools/deploy.sh                                # :latest
# откройте http://<IP>:8000 → вход admin / admin → задайте свои логин и пароль
docker compose exec dashboard python -m app.cli seed     # по желанию
```

**Что переживает обновление.** Всё состояние — в `./data/homelab.db` на хосте, вне образа. При старте контейнер только применяет новые миграции (добавляют таблицы/столбцы, существующие строки не трогают), создаёт недостающие встроенные группы (права существующих групп не меняет) и создаёт `admin`/`admin` лишь на пустой БД. Поэтому `tools/deploy.sh`, `docker compose up -d --force-recreate`, `docker compose down && up` и перезагрузка хоста сохраняют пользователей, права и данные Jira, Confluence, календаря, топологии и серверов. Данные теряются только при удалении `./data` — для этого и есть бэкапы.

| Что | Где |
|---|---|
| БД, бэкапы | `./data` → `/data` в контейнере. Владелец — пользователь, от которого запущен `deploy.sh` (подставляется `id -u`) |
| SSH-ключи, `known_hosts` | `./ssh` → `/home/app/.ssh` (только чтение). В настройках сервера путь к ключу: `/home/app/.ssh/id_ed25519` |
| Часовой пояс | `/etc/localtime` хоста; другой — `TZ=…` в `.env` |
| Порт | `DASHBOARD_BIND:DASHBOARD_PORT` из `.env`, по умолчанию `127.0.0.1:8000` |

Контейнер работает от непривилегированного пользователя, с корневой ФС только для чтения, без capabilities; healthcheck — `/healthz`.

Если данные и `.env` принадлежат одному пользователю, а Docker доступен другому: задайте в `.env` `DASHBOARD_UID`/`DASHBOARD_GID` владельца данных и запускайте `sudo bash tools/deploy.sh …` (от root контейнер не запустится — скрипт требует явный UID).

**Сервер «local» в Docker.** psutil видит память хоста, а из дисков — разделы, смонтированные в контейнер (раздел с `./data`). Для полной картины дисков мини-ПК переключите его на `ssh`-сбор с адресом `host.docker.internal` (или IP хоста в LAN). SSH-проверка `127.0.0.1` внутри контейнера стучится в сам контейнер — укажите тот же адрес хоста.

### Бэкап и перенос на другое устройство

```bash
docker compose exec dashboard python -m app.cli backup      # → data/backups/homelab-ДАТА.db, хранится 14 последних
```

Копия снимается на ходу (SQLite backup API), проверяется `integrity_check`, права 0600. Без Docker: `.venv/bin/python -m app.cli backup`. Для регулярных копий — cron на хосте, например `0 3 * * * cd /srv/homelab && docker compose exec -T dashboard python -m app.cli backup`.

Перенос: остановите контейнер (`docker compose stop`), скопируйте каталог `data/` и `.env` на новое устройство в клон репозитория, выполните `tools/deploy.sh` (или `build`). **`SECRET_KEY` переносится вместе с БД** — без него пути к SSH-ключам не расшифруются. Восстановление из бэкапа: остановить контейнер → `cp data/backups/homelab-….db data/homelab.db` → удалить `data/homelab.db-wal` и `-shm` → запустить.

## Внешний сервер и защита данных

Перед выкладкой на сервер, доступный из интернета:

1. **Первый вход не с `admin/admin`.** Задайте `FIRST_ADMIN_PASSWORD` (длинный) до первого запуска — или запустите первый раз с `DASHBOARD_BIND=127.0.0.1` и зайдите через SSH-туннель: `ssh -L 8000:127.0.0.1:8000 сервер`.
2. **Только HTTPS.** Наружу открывайте прокси, а не порт 8000. Пример Caddy (сертификат Let's Encrypt получает сам):
   ```
   dash.example.ru {
       reverse_proxy 127.0.0.1:8000
   }
   ```
   В `.env`: `COOKIE_SECURE=true` (включает и HSTS), `DASHBOARD_BIND=127.0.0.1`.
3. **Секреты — файлами, отдельно от данных.** В `.env`: `COMPOSE_FILE=compose.yaml:compose.secrets.yaml` и `SECRETS_DIR=/путь/вне/data`; положите туда `secret_key` (и `first_admin_password`), права 0600, уберите `SECRET_KEY` из `.env`. Так ключ не виден в `docker inspect`, а копия `data/` без него не расшифрует пути к ключам.
4. **SSH-ключ дашборда ограничен на опрашиваемых машинах.** В `~/.ssh/authorized_keys` пользователя мониторинга на Raspberry Pi и других хостах:
   ```
   from="IP_ДАШБОРДА",command="df -B1 -x tmpfs -x devtmpfs -x squashfs; free -b",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA… homelab-dashboard
   ```
   Украденный с сервера ключ сможет выполнить только эту команду и только с адреса дашборда.
5. **Шифрование данных приложения** — см. ниже: вся БД (SQLCipher), бэкапы (age), «перец» паролей.
6. **Шифрование диска** сервера (LUKS или шифрованный том у хостера) с разблокировкой не с самого сервера — защищает БД, бэкапы, ключи и логи при краже диска или снапшота.
7. **Пароли не храните в статьях Confluence**: история версий сохраняет всё, что когда-либо вставили. Для паролей — менеджер паролей.

### Шифрование данных

Все три механизма включаются по отдельности и по умолчанию выключены — текущая установка продолжает работать как есть. Ключи храните **не** рядом с `data/` (лучше всего — `compose.secrets.yaml` и `SECRETS_DIR` на другом томе) и **отдельно сохраните их копию** (менеджер паролей): без ключа БД и бэкапы не восстановить.

| Что | Переменная | Защищает от | Если ключ потерян |
|---|---|---|---|
| Вся БД — SQLCipher (AES-256): статьи с историей, задачи, события, пользователи, топология, журнал | `DB_ENCRYPTION_KEY` (64 hex) | утечки файла БД, `-wal`, бэкапа, снапшота диска | данные не восстановить |
| Бэкапы — age, открытым ключом | `BACKUP_AGE_RECIPIENT` (`age1…`) | утечки бэкапов даже при взломе сервера: приватный ключ только у вас | бэкапы не расшифровать |
| «Перец» паролей: HMAC перед argon2id | `PASSWORD_PEPPER` | перебора паролей по утёкшему файлу БД | никто не войдёт → `app.cli create-admin` |

**Ключи разом:** `.venv/bin/python tools/setup_secrets.py /путь/к/каталогу-секретов` — создаёт каталог 0700 и файлы `secret_key` (переносит текущий `SECRET_KEY`), `db_encryption_key`, `password_pepper`, `first_admin_password`, ничего не перезаписывает и не печатает ключи (только отпечатки). Лучше разнести по разным дискам: БД — на одном, `SECRETS_DIR` — на другом, `BACKUP_DIR` (бэкапы) — на третьем: тогда утечка одного диска не даёт ни данных, ни возможности их расшифровать.

**Включить шифрование БД** (на существующей установке — однократная конвертация):

```bash
python3 -c "import secrets; print(secrets.token_hex(32))" > secrets/db_encryption_key   # или tools/setup_secrets.py
chmod 600 secrets/db_encryption_key
docker compose exec dashboard python -m app.cli backup --no-age                        # страховка
docker compose stop dashboard
docker compose run --rm dashboard python -m app.cli encrypt-db                         # с compose.secrets.yaml
docker compose up -d dashboard
```

Без Docker: `DB_ENCRYPTION_KEY_FILE=/путь/к/ключу .venv/bin/python -m app.cli encrypt-db` при остановленном сервисе. Команда сверяет число строк в каждой таблице, затирает открытый файл и предупреждает о старых открытых бэкапах в `data/backups` — их удалите. Новая установка с заданным ключом сразу создаёт зашифрованную БД. Поиск, фильтры и все разделы работают как прежде.

**Шифрование бэкапов.** На своём ПК (не на сервере): `age-keygen -o ~/homelab-backup.key` — команда выведет публичный ключ `age1…`; впишите его в `.env` как `BACKUP_AGE_RECIPIENT`. `app.cli backup` будет писать `homelab-ДАТА.db.age`, открытая копия на диск не ложится. Восстановление: `age -d -i ~/homelab-backup.key homelab-ДАТА.db.age > homelab.db` (если включён SQLCipher, копия внутри по-прежнему зашифрована `DB_ENCRYPTION_KEY`). Бэкап перед выкладкой в `deploy.sh` делается без age — чтобы откат мог восстановить его на месте.

**Перец.** `python3 -c "import secrets; print(secrets.token_hex(32))" > secrets/password_pepper`. Существующие пароли продолжают работать и переводятся на перец при следующем входе каждого пользователя (`tools/audit.py` показывает, сколько осталось).

**Почему не SHA-256 для паролей:** хранить хеш и сравнивать с хешем введённого пароля — ровно то, что делает argon2id, но он требует 64 МБ памяти и ~150 мс на попытку. SHA-256 на той же машине проверяет ~450 000 паролей в секунду на ядро (на видеокарте — миллиарды): утёкшая база с SHA-256 перебирается по словарю за минуты.

Что ещё сделано в приложении: пароли — argon2id (не SHA-256: он в десятки тысяч раз быстрее для перебора); удалённые строки затираются (`secure_delete`), `-wal` раз в час переносится в БД и обрезается; в журнал входов не пишется строка из поля логина, если такого пользователя нет (туда часто по ошибке набирают пароль); в access-лог не попадают поисковые запросы; заголовки `Permissions-Policy` и (за HTTPS) `Strict-Transport-Security`.

## CI/CD

`.github/workflows/ci.yml` — на каждый push в `Dashboard`/`main`, теги `v*` и PR:
1. тесты, проверка `requirements.lock`, `tools/audit.py` (bandit, pip-audit, секреты в git);
2. сборка образа, smoke-тест (контейнер с read-only ФС отвечает на `/healthz` и `/login`), Trivy — без критических CVE;
3. push владельца в основную ветку (не PR и не `dev`): публикация `ghcr.io/spxrtxn/infrastructurepanel` для amd64 и arm64 с тегами `latest` (основная ветка и релизы), имя ветки, `sha-…`, версия из тега `v1.2.0` → `1.2.0`.

Релиз: `git tag v1.0.0 && git push origin v1.0.0`.

`.github/workflows/deploy.yml` — выкладка кнопкой **Actions → Deploy → Run workflow** (тег на выбор). GitHub не видит домашнюю сеть, поэтому задание выполняет self-hosted runner на сервере:
1. на сервере: **Settings → Actions → Runners → New self-hosted runner**, при регистрации добавьте метку `homelab`; пользователь раннера — в группе `docker`;
2. подготовьте каталог развёртывания (как в разделе «Docker»: `.env`, `data/`, `ssh/`) и задайте переменную репозитория **Settings → Secrets and variables → Actions → Variables → `DEPLOY_DIR`** = путь к нему;
3. задание копирует туда `compose.yaml` и `tools/deploy.sh` и запускает выкладку: бэкап → pull → up → ожидание healthy → при ошибке откат на прошлый образ с восстановлением бэкапа.

Без раннера то же самое делается на сервере вручную: `git pull && tools/deploy.sh`.

`.github/dependabot.yml` — еженедельные PR с обновлениями pip, базового образа и actions. После обновления пакетов из `requirements.txt` пересоберите lock: `.venv/bin/python tools/lock_deps.py`.

## Мониторинг по SSH (например, Raspberry Pi)

1. Создайте на удалённой машине пользователя с доступом только на чтение и добавьте ему публичный ключ.
2. Добавьте ключ хоста в `known_hosts` пользователя, от которого запущен дашборд: `ssh-keyscan 192.168.1.10 >> ~/.ssh/known_hosts` (неизвестный ключ хоста считается ошибкой — это защита от подмены).
3. В **Настройки → Серверы и сервисы** укажите пользователя SSH, путь к приватному ключу и включите сервер.

Выполняется одна команда: `df -B1 -x tmpfs -x devtmpfs -x squashfs; free -b`. Путь к ключу хранится зашифрованным и не показывается в интерфейсе и API.

## Права

Право — строка `<раздел>.view` / `<раздел>.edit` (servers, jira, confluence, calendar, topology), а также `settings.manage` и `users.manage`.

| Группа | Права |
|---|---|
| Администраторы | всё |
| Пользователи | просмотр и изменение во всех разделах, без настроек и пользователей |
| Наблюдатели | только просмотр |

Пункт меню без `.view` скрыт, а каждый обработчик проверяет права на сервере.

## Безопасность

- Пароли — argon2id, не попадают в логи и ответы (включая ошибки валидации).
- Сессия — случайный токен в cookie `HttpOnly`, `SameSite=Lax`, `Secure` по `COOKIE_SECURE`; в БД хранится только хеш токена.
- CSRF-токен на всех изменяющих запросах (глобальная проверка).
- Блокировка перебора: 5 неудачных попыток за 15 минут для пары логин+IP; журнал входов — в **Настройки → Пользователи**.
- Markdown рендерится на сервере без сырого HTML и санитизируется (nh3).
- Заголовки: CSP (`default-src 'self'`, скрипты только свои, без inline), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`.

Автоматическая проверка чек-листа — `tests/test_security_checklist.py`, аудит конфигурации — `tools/audit.py`.

## Разработка

```bash
.venv/bin/pip install -r requirements-dev.txt                   # + pytest, pip-audit, bandit
.venv/bin/python tools/lock_deps.py                              # после изменения requirements.txt
.venv/bin/python tools/audit.py                                  # аудит: БД, права, секреты, зависимости, тесты безопасности
.venv/bin/pytest -q                                              # тесты на временной БД
.venv/bin/alembic revision --autogenerate -m "..." --rev-id 0007  # новая миграция после изменения моделей
.venv/bin/python tools/build_icons.py                           # пересобрать icons.svg после добавления иконки
```

Тест `test_migrations_match_models` упадёт, если миграцию забыли создать.


```
app/        main.py config.py db.py cli.py web.py
            models/ routers/ services/ templates/ static/
            guide/ — руководство пользователя (в панели: Confluence → «Руководство»)
migrations/ Alembic
tests/      pytest
tools/      деплой, релизы, аудит, обслуживание
deploy/     нативная установка без Docker
handoff/homelab-agent/  агент узлов (отдельный проект, своя версия)
```
