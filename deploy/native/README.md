# Нативная установка Homelab Dashboard (без Docker)

Панель как служба systemd на любом Linux-узле: Raspberry Pi 4B (1 ГБ, arm64, Raspberry Pi OS bookworm,
Python 3.11), отдельный «узел администрирования» (amd64/arm64, Debian 12+/Ubuntu 24.04+). Основной сервер
можно и дальше держать в Docker (`tools/deploy.sh`) — это независимые варианты.

| Что | Где | Права |
|---|---|---|
| Код и venv | `/opt/homelab-dashboard` | root, 0755 |
| БД, бэкапы, `~/.ssh` службы | `/var/lib/homelab-dashboard` | homelab-dashboard, 0700 |
| Окружение службы | `/etc/homelab-dashboard/env` | root, 0600 |
| Ключи (Fernet, SQLCipher, перец, пароль первого входа) | `/etc/homelab-dashboard/secrets/` | homelab-dashboard, 0700/0600 |
| Юнит | `/etc/systemd/system/homelab-dashboard.service` (+ `.d/10-resources.conf`) | root |

Файлы: `install.sh` — установка/обновление, `homelab-dashboard.service` — юнит, `env.example` — шаблон окружения,
`homelab-dashboard-cli` — обёртка для `python -m app.cli` (бэкап, создание админа), `tests/` — статические проверки.

## Установка на Raspberry Pi 4B
```bash
sudo apt update && sudo apt install -y git
git clone <URL репозитория дашборда> ~/homelab-dashboard
cd ~/homelab-dashboard
sudo deploy/native/install.sh
```
Скрипт: ставит системные пакеты (`python3-venv`, `tzdata`), создаёт пользователя `homelab-dashboard` и каталоги,
venv (`pip install --only-binary=:all: -r requirements.txt -c requirements.lock` — только готовые сборки, без
компилятора; наличие проверяет `tools/check_wheels.py` для Python 3.11–3.13, amd64 и arm64), генерирует ключи
(`tools/setup_secrets.py`, на экран — только отпечатки), применяет миграции (`alembic upgrade head`), ставит юнит
с потолком памяти по объёму RAM (1 ГБ → `MemoryMax=320M`) и запускает службу.

Без клонирования на узел: `sudo ./install.sh --git <URL> --ref main` (скрипт сам сделает `git clone`).

Первый вход: логин `admin`, пароль — `sudo cat /etc/homelab-dashboard/secrets/first_admin_password`;
панель сразу попросит сменить логин и пароль. Либо создать своего администратора (пароль вводится интерактивно):
```bash
sudo homelab-dashboard-cli create-admin
```
`homelab-dashboard-cli` (ставится в `/usr/local/sbin`) — это `python -m app.cli …` от имени службы и с её окружением.

### Доступ из сети
По умолчанию `HOST=127.0.0.1` — панель видна только на самом узле. Варианты:
- reverse proxy с HTTPS (nginx/Caddy) на этом узле → `HOST=127.0.0.1`, `COOKIE_SECURE=true`;
- в локальной сети без HTTPS → `HOST=0.0.0.0` (трафик и пароль идут открытым текстом — только для доверенной сети).

```bash
sudoedit /etc/homelab-dashboard/env
sudo systemctl restart homelab-dashboard
```

## Установка на узел администрирования (amd64/arm64)
То же самое: `sudo deploy/native/install.sh`.
Если узел будет собирать метрики с других серверов по SSH — ключ кладите в `/var/lib/homelab-dashboard/.ssh/`
(владелец `homelab-dashboard`, 0600), отпечатки хостов — в `/var/lib/homelab-dashboard/.ssh/known_hosts`;
в панели указывайте путь к ключу.

## Обновление
```bash
cd ~/homelab-dashboard && git pull
sudo deploy/native/install.sh --upgrade
```
Порядок: бэкап БД старым кодом (`python -m app.cli backup --no-age`, копия в `/var/lib/homelab-dashboard/backups`)
→ остановка службы → новый код → зависимости → `alembic upgrade head` → запуск. Если бэкап не удался, обновление
не начинается. env, секреты и БД не перезаписываются. С `--git <URL>` код берётся из клона.

## Бэкап
```bash
sudo homelab-dashboard-cli backup              # горячая копия, служба может работать
sudo homelab-dashboard-cli backup --keep 30    # хранить 30 последних
```
Ежедневно — через cron root: `echo '30 3 * * * root /usr/local/sbin/homelab-dashboard-cli backup >/dev/null' | sudo tee /etc/cron.d/homelab-dashboard-backup`.
- Копии — `/var/lib/homelab-dashboard/backups/homelab-*.db` (последние 14), зашифрованы тем же
  `DB_ENCRYPTION_KEY`; с `BACKUP_AGE_RECIPIENT` — ещё и age (`.db.age`).
- Каталог на другом диске: `BACKUP_DIR=/mnt/backup/homelab-dashboard` в env и разрешение на запись:
  `sudo systemctl edit homelab-dashboard` → `[Service]` `ReadWritePaths=/mnt/backup/homelab-dashboard`.
- **Ключи** из `/etc/homelab-dashboard/secrets/` храните отдельно (менеджер паролей): без них БД и бэкапы не открыть.

Восстановление:
```bash
sudo systemctl stop homelab-dashboard
sudo install -m 0600 -o homelab-dashboard -g homelab-dashboard \
    /var/lib/homelab-dashboard/backups/homelab-ГГГГММДД-ЧЧММСС.db /var/lib/homelab-dashboard/homelab.db
sudo rm -f /var/lib/homelab-dashboard/homelab.db-wal /var/lib/homelab-dashboard/homelab.db-shm
sudo systemctl start homelab-dashboard
```

Перенос из Docker: остановите контейнер, скопируйте `homelab.db` и ключи (`SECRET_KEY`, `DB_ENCRYPTION_KEY`,
`PASSWORD_PEPPER`) в файлы `/etc/homelab-dashboard/secrets/` **до** первого запуска `install.sh`
(существующие файлы он не трогает), БД — в `/var/lib/homelab-dashboard/`. Если БД открытая (без SQLCipher), а ключ
шифрования уже задан — один раз при остановленной службе: `sudo homelab-dashboard-cli encrypt-db`.

## Удаление
```bash
sudo systemctl disable --now homelab-dashboard
sudo rm -r /etc/systemd/system/homelab-dashboard.service /etc/systemd/system/homelab-dashboard.service.d
sudo systemctl daemon-reload
sudo rm -r -f /opt/homelab-dashboard /usr/local/sbin/homelab-dashboard-cli /etc/cron.d/homelab-dashboard-backup
# Данные и ключи — только если они точно не нужны (сначала сохраните бэкап и ключи):
sudo rm -r /var/lib/homelab-dashboard /etc/homelab-dashboard
sudo userdel homelab-dashboard
```

## Рекомендации для Raspberry Pi с 1 ГБ RAM
- Потолок памяти службы — `MemoryMax=320M` (drop-in `10-resources.conf`); поменять: `sudo deploy/native/install.sh --upgrade --memory-max 400M`.
- Один процесс uvicorn (так и задумано: планировщик внутри приложения). Не ставьте рядом Docker и тяжёлые сервисы.
- Включите zram вместо swap на SD-карте: `sudo apt install zram-tools` (по умолчанию 50 % RAM, lz4).
- БД и бэкапы лучше держать на USB-SSD, а не на SD-карте (износ): `DATABASE_URL=sqlite:////mnt/ssd/homelab/homelab.db`
  и `ReadWritePaths=/mnt/ssd/homelab` через `systemctl edit`.
- Ограничьте журнал: в `/etc/systemd/journald.conf` — `SystemMaxUse=100M`.
- Интервал проверки серверов в «Настройки → Серверы» не делайте меньше 60 с.
- Агент мониторинга самого Pi — `handoff/homelab-agent` (≈ 30 МБ RAM), он независим от панели.

## Ограничения службы (юнит)
`NoNewPrivileges`, `ProtectSystem=strict` (запись только в `/var/lib/homelab-dashboard`), `ProtectHome=read-only`
(чтобы psutil видел реальный размер `/home`), `PrivateTmp`, `PrivateDevices`, `ProtectKernel*`, `RestrictAddressFamilies`,
`SystemCallFilter=@system-service`, пустой `CapabilityBoundingSet`, `UMask=0077`, `Restart=on-failure`.
Секреты — файлами `*_FILE`, а не переменными окружения (не видны в `/proc/<pid>/environ`).

Проверка юнита на узле: `systemd-analyze security homelab-dashboard` и `systemd-analyze verify /etc/systemd/system/homelab-dashboard.service`.

## Решения (кратко)
- **Секреты — файлы `*_FILE` в `/etc/homelab-dashboard/secrets` (владелец — служба), env — только пути (root, 0600)** — env читает systemd до сброса прав, ключи не видны в окружении процесса — ключи прямо в env.
- **Пароль первого входа — случайный, в `secrets/first_admin_password`** — нативный узел может быть виден в сети, admin/admin опасен — пустой файл (admin/admin) из `setup_secrets.py`.
- **`MemoryMax` — drop-in по объёму RAM при установке** — systemd не подставляет переменные из EnvironmentFile в лимиты ресурсов — значение в env-шаблоне.
- **`ProtectHome=read-only`, а не `true`** — psutil должен видеть настоящий раздел `/home` для метрик своего узла — полная изоляция /home.
- **`HOMELAB_IGNORE_DOTENV=1` в юните и обёртке** — случайный `.env` в каталоге кода не перекроет настройки службы — читать и .env.
- **Бэкап при `--upgrade` — старым кодом, до замены файлов; без age** — откат возможен на самом узле без приватного ключа age — бэкап после обновления.
- **Служба останавливается на время обновления** — миграции alembic не конкурируют с работающим приложением — обновление «на ходу».
- **Не проверено на реальном Pi/systemd** (среда разработки без sudo и systemd): только `bash -n` и статические тесты `deploy/native/tests`. Совместимость `requirements.lock` с Python 3.11 проверена `pip install --dry-run --python-version 3.11 --only-binary=:all:` (x86_64): все версии разрешаются. Для arm64 не проверено — там `sqlcipher3` собирается из исходников (нужны пакеты, которые ставит скрипт).
