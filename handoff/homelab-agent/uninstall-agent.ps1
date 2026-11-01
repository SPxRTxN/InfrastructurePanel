# Полное удаление homelab-agent из Windows: правой кнопкой → «Выполнить с помощью PowerShell».
# Убирает задачу планировщика, программу и данные (конфиг, токен, журнал). Python не трогает.
# Токен узла после этого отзовите в панели (Инфраструктура → Узлы → узел → Удалить).
$ErrorActionPreference = "Continue"
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit 0
}
$answer = Read-Host "Удалить homelab-agent, его конфиг и токен с этого компьютера? [y/N]"
if ($answer -notmatch '^[yYдД]$') { Write-Host "Отменено"; exit 0 }

Write-Host "==> Задача планировщика"
Stop-ScheduledTask -TaskName "homelab-agent" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "homelab-agent" -Confirm:$false -ErrorAction SilentlyContinue
# pythonw агента мог остаться, если задача завершалась с ошибкой
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*homelab_agent*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Write-Host "==> Файлы"
Remove-Item -Recurse -Force (Join-Path $env:ProgramFiles "homelab-agent") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $env:ProgramData "homelab-agent") -ErrorAction SilentlyContinue

Write-Host "==> Готово. Осталось отозвать токен узла в панели. Python, если ставился ради агента, удаляется"
Write-Host "    в «Параметры → Приложения» (Python 3.12) или: winget uninstall -e --id Python.Python.3.12"
Read-Host "Enter — закрыть"
