# homelab-agent

**Что делает и что умеет — [docs/Возможности.md](docs/Возможности.md).** История версий — [CHANGELOG.md](CHANGELOG.md).

Лёгкий агент мониторинга и восстановления для узлов домашней лаборатории (Linux, в том числе
Raspberry Pi 4B с 1 ГБ RAM) и драйвер OLED-дисплея. Отправляет телеметрию в одну или несколько панелей
Homelab Dashboard по `wss://…/agent/ws` и выполняет **только заранее разрешённые на узле** действия
восстановления (панель присылает лишь имя действия).

- ТЗ — `docs/ТЗ.md`, протокол — `docs/Протокол.md`.- Python 3.11+, зависимости: `psutil`, `websockets`; для дисплея — `pillow`, `luma.oled`.
- RSS после импорта ~29 МБ (с дисплеем ~33 МБ; замер на x86_64, Python 3.12), сбор раз в 10 с — доли процента CPU.

## Быстрая установка (любой узел)
1. В панели: **Инфраструктура → Узлы → добавить узел** — скопировать токен.
2. С сервера панели (или любого ПК с SSH до узла) скопировать агента:
   `sudo bash /opt/homelab-dashboard/tools/push-agent.sh user@узел` (из этого каталога — `./push-agent user@узел`).
3. На узле вписать токен в начало установщика и запустить:
   - **Linux** (Debian/Ubuntu/Raspberry Pi OS, Arch, Fedora, openSUSE; нужен systemd):
     `cd ~/homelab-agent && nano start-agent` → `TOKEN="…"`, `NODE="…"` → `./start-agent`
   - **Windows 10/11 / Server**: `homelab-agent\start-agent.ps1` → `$TOKEN = "…"` → ПКМ → «Выполнить с помощью PowerShell».

   Установщик ставит Python (apt/pacman/dnf/zypper/winget), службу с автозапуском (systemd / задача планировщика от
   «Локальной службы»), кладёт токен в защищённый файл и стирает его из установщика. Дисплей включается сам, если
   есть `/dev/i2c-*`. Повторный запуск — обновление.
4. Удаление — `./uninstall-agent` / `uninstall-agent.ps1`, подробно — [УДАЛЕНИЕ.md](УДАЛЕНИЕ.md).

Windows: без температур, systemd и дисплея; CPU, RAM, диски (`C:`…), время работы, проверки, свои команды — как в Linux.
Журнал — `C:\ProgramData\homelab-agent\logs\agent.log`. Alpine/OpenRC — пока вручную (раздел ниже, свой init-скрипт).

## Что собирает
| Коллектор | Датчики |
|---|---|
| cpu | `cpu.percent`, `cpu.load1/5/15`, `cpu.freq_mhz` |
| memory | `memory.total`, `memory.used`, `memory.available` (байты) |
| disks | `disks.<точка монтирования>.total/used/free` (байты, как `df`, только реальные ФС) |
| thermal | `thermal.<имя>.temp` (°C); на Pi — `thermal.cpu.temp` |
| uptime | `uptime.seconds` |
| processes | `processes.<name>.running/cpu/rss` |
| systemd | `systemd.<unit>.active` (1/0) |
| checks | `checks.<name>.up/latency_ms` (TCP, HTTP) |
| command | `command.<name>.value` — своя команда из конфига, число из вывода |

Без секции `[collectors]` включены cpu, memory, disks, thermal, uptime.
- `agent.example.toml` — рабочий минимум, его копирует `install.sh`: одна панель, базовые коллекторы; поменять
  `node` (как имя узла в панели) и `url`.
- `agent.full.toml` — справочник всех настроек (процессы, службы, проверки, свои команды, действия, вторая панель);
  адреса и сервисы в нём — примеры, переносите нужное по одному.

## Установка на Raspberry Pi 4B (Raspberry Pi OS bookworm)
```bash
sudo apt install -y python3-venv git
git clone <репозиторий homelab-agent> ~/homelab-agent      # или скопируйте каталог
cd ~/homelab-agent
# Только для дисплея: включить I2C и проверить адрес (обычно 3c)
sudo raspi-config nonint do_i2c 0
sudo apt install -y i2c-tools && sudo i2cdetect -y 1
sudo ./packaging/install.sh --display        # без дисплея — без --display
```
Скрипт создаёт пользователя `homelab-agent` и папку `/opt/homelab-agent` (см. «Раскладка на узле»), конфиг
`etc/agent.toml` из примера и юнит `homelab-agent.service` (не включает его).
Дальше — шаги, которые он печатает в конце:
```bash
# 1. Токен узла (панель → «Инфраструктура» → «Узлы» → добавить узел). Вставить в терминал, Enter, Ctrl-D
#    (Ctrl+Shift+V для вставки; не используйте sudo -i — у служебного пользователя нет входа):
sudo sh -c 'umask 077; cat > /opt/homelab-agent/etc/token-main'
sudo chown homelab-agent: /opt/homelab-agent/etc/token-main
# 2. Конфиг: url панели, коллекторы, действия, [display] enabled = true
sudoedit /opt/homelab-agent/etc/agent.toml
# 3. Права на действия восстановления (если есть [actions.*]) — по образцу bin/sudoers.example; свои строки
#    добавляйте в тот же файл вне блока «managed»
sudo visudo -f /etc/sudoers.d/homelab-agent
# 4. Проверка и пробный сбор
cd /opt/homelab-agent/app/current && sudo -u homelab-agent .venv/bin/python -m homelab_agent -c /opt/homelab-agent/etc/agent.toml --check-config
cd /opt/homelab-agent/app/current && sudo -u homelab-agent .venv/bin/python -m homelab_agent -c /opt/homelab-agent/etc/agent.toml --once
# 5. Запуск
sudo systemctl enable --now homelab-agent
journalctl -u homelab-agent -f
```

## Установка на сервер (Debian 12 / Ubuntu 24.04, amd64 или arm64)
```bash
sudo apt install -y python3-venv git
git clone <репозиторий homelab-agent> ~/homelab-agent && cd ~/homelab-agent
sudo ./packaging/install.sh
```
Дальше те же шаги 1–5. Если агент должен видеть сервисы в Docker — проверяйте их через
`[collectors.checks]` (TCP/HTTP) или `systemd` (`docker.service`).

## Раскладка на узле
Всё в одной папке; вне неё только правило sudo, ссылки на юниты и пользователь `homelab-agent`.
```
/opt/homelab-agent/        root:root 0755 — иначе агент подменил бы bin/
  bin/       root 0755    uninstall-agent, guard.py (сторож), README.md, agent.*.toml, sudoers.example
  etc/       root:homelab-agent 1770 (sticky) — agent.toml (root:homelab-agent 0640), token-* (0600 агент),
             panel.toml (0600, настройки из панели), vpn/ (0700)
  app/       homelab-agent — releases/<версия>/, current, previous (переключает обновление из панели и сторож)
  state/     homelab-agent — отметки сторожа (connected, switch.json, rollback.json)
  systemd/   root         — юниты; в /etc/systemd/system лежат ссылки на них
/etc/sudoers.d/homelab-agent   один файл: перезапуск службы, uninstall-agent --from-panel (+ ваши правила действий)
```
- Юниты не копируются в `/etc/systemd/system`, а подключаются ссылками (как `systemctl link`, но замена атомарная,
  в том числе поверх файла прежней установки): так юнит нельзя подменить из агента, а удаление — это папка + ссылки.
- `ReadWritePaths` службы — только `etc/`, `app/`, `state/`; `bin/` и `systemd/` агенту недоступны для записи.
- Старая раскладка (`/etc/homelab-agent`, `/var/lib/homelab-agent`, `/usr/local/lib/homelab-agent`,
  `/opt/homelab-agent/{releases,current,previous}`, `/etc/sudoers.d/homelab-agent-update`) переносится
  `install.sh --upgrade` (его вызывает `start-agent`): копирование и проверка, старое удаляется только после успеха;
  пути `/etc/homelab-agent/…` в `agent.toml` заменяются (копия — `agent.toml.pre-migrate`). Запуск повторяем.
  Пока перенос не сделан, агент берёт конфиг по запасному пути `/etc/homelab-agent/agent.toml`.
- `--upgrade` дописывает в `agent.toml` раздел `[update] enabled = true`, если его не было.
- Настройки и удаление из панели: `[remote] config` / `uninstall` (по умолчанию `true`) — выключатели на узле;
  панель меняет только `interval`, `collectors`, `display`, `wol`, `update` (в `etc/panel.toml`), но не `panel`, `actions`,
  `vpn`, `collectors.command`, `update.restart_argv`. Протокол — `docs/Протокол.md`.
- Несколько узлов одной командой: `./push-agent --install user@host1 user@host2` — копирует и запускает
  `ssh -t host sudo bash ~/homelab-agent/start-agent` (пароль sudo вводите вы).

## Панель с самоподписанным сертификатом
Положите сертификат CA панели в `/opt/homelab-agent/etc/ca.pem` и укажите в секции панели `ca_file = "/opt/homelab-agent/etc/ca.pem"`.
Проверка сертификата не отключается; `insecure = true` лишь разрешает `ws://` для локальной отладки.

## Обновление
```bash
cd ~/homelab-agent && git pull
sudo ./packaging/install.sh --upgrade
```
Конфиг и токены не перезаписываются; если на узле ставили `--display`, дисплейные пакеты обновятся тоже.

## Удаление
Проще всего `./uninstall-agent` из каталога установщика или кнопка в панели (удаление узла с галочкой).
Вручную — подробно в `УДАЛЕНИЕ.md`; токен узла отзовите в панели.

## Отладка без панели
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m homelab_agent -c agent.toml --once          # один сбор, JSON в stdout
.venv/bin/python -m homelab_agent -c agent.toml --check-config  # конфиг и права на токены
.venv/bin/pytest -q                                             # тесты (без сети и железа)
```

## Расширение
- Новый источник данных — файл в `homelab_agent/collectors/` с `@register("имя")` и секцией `[collectors.имя]`.
- Новый экран — файл в `homelab_agent/display/screens/` с `@screen("имя")`, имя — в `[display] screens`.
- Новое действие восстановления — секция `[actions.имя]` в конфиге узла + строка в sudoers, без кода.

## Безопасность (кратко)
- Только `wss://` с проверкой сертификата; токен — в файле 0600, не логируется и не попадает в `ps`.
- Панель не может прислать команду: только имя действия из `[actions.*]`; запуск без shell, с таймаутом,
  лимитом частоты, одним действием за раз, выводом ≤ 4 КБ.
- `install.sh` не скачивает ничего, кроме пакетов pip из `requirements*.txt` (фиксированные версии).
- Агент не запускается под root; привилегии — строки sudoers ровно на нужные команды.
- Юнит: `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `DevicePolicy=closed` (+ I2C), `MemoryMax=96M`.
  `NoNewPrivileges` выключен из-за sudo — если действий нет, включите его.


## Перенос в отдельный репозиторий
Каталог подготовлен в репозитории дашборда (`handoff/homelab-agent`). Чтобы вести агент отдельно:
```bash
cp -r /opt/homelab-dashboard/handoff/homelab-agent ~/homelab-agent
cd ~/homelab-agent && git init -b main && git add . && git commit -m "homelab-agent 0.1.0"
```
