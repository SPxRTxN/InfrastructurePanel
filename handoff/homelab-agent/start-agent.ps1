# Установка и запуск homelab-agent в Windows 10/11 и Windows Server 2019+.
#
# 1. Впишите токен узла ниже (панель → Инфраструктура → Узлы → добавить узел) — или оставьте пустым,
#    тогда скрипт спросит его.
# 2. Правой кнопкой по файлу → «Выполнить с помощью PowerShell» (или: powershell -ExecutionPolicy Bypass -File .\start-agent.ps1).
#    Скрипт попросит права администратора, при необходимости поставит Python (winget), создаст задачу
#    планировщика «homelab-agent» (запуск при загрузке от «Локальной службы», перезапуск при сбое),
#    запишет токен в C:\ProgramData\homelab-agent\token-main (доступ: SYSTEM, администраторы, агент)
#    и сотрёт его из этого файла. Повторный запуск — обновление.
# Удаление — uninstall-agent.ps1 (подробно в УДАЛЕНИЕ.md).
# В Windows нет температур (без сторонних драйверов), systemd и дисплея; остальное — как в Linux.

# Запуск из панели (команда установки для Windows) — без вопросов:
#   start-agent.ps1 -NodeName <узел> -PanelWs <wss://…/agent/ws> -TokenPath <файл с токеном> -Unattended
# Имена параметров не совпадают с $NODE/$TOKEN: в PowerShell имена переменных без учёта регистра.
param([string]$NodeName = "", [string]$PanelWs = "", [string]$TokenPath = "", [switch]$Unattended)
$TOKEN = ""
$NODE = ""                                       # пусто — имя компьютера; должно совпадать с именем узла в панели
$PANEL_URL = "ws://192.168.1.39:8000/agent/ws"  # за HTTPS-прокси — wss://…

$ErrorActionPreference = "Stop"
if ($NodeName) { $NODE = $NodeName }
if ($PanelWs) { $PANEL_URL = $PanelWs }
if ($TokenPath) { $TOKEN = ([IO.File]::ReadAllText($TokenPath)).Trim() }
$Here = Split-Path -Parent $PSCommandPath
$Prefix = Join-Path $env:ProgramFiles "homelab-agent"
$Data = Join-Path $env:ProgramData "homelab-agent"
$TaskName = "homelab-agent"

function Say($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Die($text) { Write-Host "ОШИБКА: $text" -ForegroundColor Red; if (-not $Unattended) { Read-Host "Enter — выход" }; exit 1 }

# --- права администратора ---
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    if ($Unattended) { Die "нужен PowerShell от имени администратора (ПКМ по «Пуск» → «Терминал (администратор)»)" }
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit 0
}

# --- проверки ввода ---
if ($TOKEN -eq "" -and -not (Test-Path (Join-Path $Data "token-main"))) {
    $secure = Read-Host "Токен узла из панели" -AsSecureString
    $TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
if ($TOKEN -ne "" -and $TOKEN -notmatch '^[A-Za-z0-9_-]{20,200}$') { Die "токен похож на неверный — скопируйте его заново" }
if ($NODE -eq "") { $NODE = $env:COMPUTERNAME.ToLower() }
if ($NODE -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') { Die "NODE: латиница, цифры, . _ -" }
if ($PANEL_URL -notmatch '^wss?://[A-Za-z0-9.:_/-]+$') { Die "PANEL_URL должен начинаться с ws:// или wss://" }

# --- Python 3.11+, установленный для всех пользователей (иначе «Локальная служба» его не увидит) ---
function Find-Python {
    foreach ($cmd in @("py", "python")) {
        $exe = Get-Command $cmd -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $pyArgs = if ($cmd -eq "py") { @("-3", "-c") } else { @("-c") }
        try {
            $path = & $exe.Source @pyArgs "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
        } catch { continue }
        if ($path -and -not $path.StartsWith($env:USERPROFILE) -and -not $path.Contains("WindowsApps")) { return $path.Trim() }
    }
    foreach ($p in Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending) {
        return $p.FullName
    }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    Say "Ставлю Python 3.12 для всех пользователей (winget)"
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { Die "нет winget — поставьте Python 3.12 с python.org (галочка «for all users») и запустите снова" }
    winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
    $Python = Find-Python
    if (-not $Python) { Die "Python не нашёлся после установки — перезапустите скрипт" }
}
Say "Python: $Python"

# --- код и окружение ---
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
Say "Код в $Prefix"
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
Remove-Item -Recurse -Force (Join-Path $Prefix "homelab_agent") -ErrorAction SilentlyContinue
Copy-Item -Recurse (Join-Path $Here "homelab_agent") $Prefix
Get-ChildItem -Recurse -Directory -Filter "__pycache__" (Join-Path $Prefix "homelab_agent") | Remove-Item -Recurse -Force
foreach ($f in @("requirements.txt", "agent.full.toml", "README.md")) { Copy-Item (Join-Path $Here $f) $Prefix -Force }
$Venv = Join-Path $Prefix ".venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) { & $Python -m venv $Venv }
Say "Зависимости"
& (Join-Path $Venv "Scripts\python.exe") -m pip install --quiet --disable-pip-version-check -r (Join-Path $Prefix "requirements.txt")
if ($LASTEXITCODE -ne 0) { Die "pip не поставил зависимости" }
# «Локальной службе» — чтение программы и самого Python (SID вместо имён: в русской Windows группы называются иначе)
$PyBase = (& $Python -c "import sys; print(sys.base_prefix)").Trim()
icacls $Prefix /grant "*S-1-5-19:(OI)(CI)RX" | Out-Null
icacls $PyBase /grant "*S-1-5-19:(OI)(CI)RX" | Out-Null

# --- данные: конфиг и токен — SYSTEM и администраторы, агент только читает; журнал — агент пишет ---
New-Item -ItemType Directory -Force -Path $Data | Out-Null
icacls $Data /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-19:(OI)(CI)RX" | Out-Null
$Logs = Join-Path $Data "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
icacls $Logs /grant "*S-1-5-19:(OI)(CI)M" | Out-Null
$Utf8NoBom = New-Object Text.UTF8Encoding $false   # tomllib не принимает BOM
$Config = Join-Path $Data "agent.toml"
if (-not (Test-Path $Config)) {
    Say "Конфиг: узел $NODE, панель $PANEL_URL"
    $insecure = if ($PANEL_URL.StartsWith("ws://")) { "insecure = true            # ws:// без TLS — только в своей LAN" } else { "" }
    $tokenPath = Join-Path $Data "token-main"
    $text = @"
# homelab-agent (Windows). Все настройки с примерами — $Prefix\agent.full.toml
node = "$NODE"
interval = 10

[[panel]]
url = "$PANEL_URL"
token_file = '$tokenPath'
$insecure

[collectors.cpu]
[collectors.memory]
[collectors.disks]
[collectors.uptime]
"@
    [IO.File]::WriteAllText($Config, $text, $Utf8NoBom)
} else {
    Write-Host "  $Config уже есть — не трогаю"
}
if ($TOKEN -ne "") {
    Say "Токен → $Data\token-main"
    [IO.File]::WriteAllText((Join-Path $Data "token-main"), $TOKEN, $Utf8NoBom)
    # Токен больше не нужен в этом файле; BOM сохраняем — без него Windows PowerShell 5.1 не прочтёт кириллицу
    if (-not $TokenPath) {
        $self = [IO.File]::ReadAllLines($PSCommandPath) -replace '^\$TOKEN = ".*"$', '$TOKEN = ""'
        [IO.File]::WriteAllLines($PSCommandPath, $self, (New-Object Text.UTF8Encoding $true))
    }
}

Say "Проверка конфига"
Push-Location $Prefix
& (Join-Path $Venv "Scripts\python.exe") -m homelab_agent -c $Config --check-config
$ok = $LASTEXITCODE
Pop-Location
if ($ok -ne 0) { Die "конфиг не прошёл проверку (поправьте $Config)" }

# --- задача планировщика: при загрузке, от «Локальной службы», перезапуск при сбое ---
Say "Автозапуск: задача планировщика $TaskName"
$account = (New-Object Security.Principal.SecurityIdentifier "S-1-5-19").Translate([Security.Principal.NTAccount]).Value
$action = New-ScheduledTaskAction -Execute (Join-Path $Venv "Scripts\pythonw.exe") -WorkingDirectory $Prefix `
    -Argument "-m homelab_agent -c `"$Config`" --log-file `"$Logs\agent.log`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType ServiceAccount -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 10
$log = Join-Path $Logs "agent.log"
if (Test-Path $log) { Get-Content -Tail 8 -Encoding UTF8 $log }
if ((Test-Path $log) -and (Select-String -Path $log -Pattern "подключено" -Quiet)) {
    Say "Готово: агент подключён к панели и запустится сам после перезагрузки"
    if ($Unattended) { exit 0 }
} else {
    Write-Host "Агент запущен, подключения ещё не видно. Журнал: $log"
    Write-Host "  bad token — не совпадают имя узла (node в $Config) или токен; ошибка соединения — адрес панели или брандмауэр."
}
if ($Unattended) { exit 2 }  # служба запущена, подключения не видно (как у start-agent: код 2)
Read-Host "Enter — закрыть"
