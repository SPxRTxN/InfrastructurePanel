"""Сетевая диагностика изнутри контейнера дашборда (в образ не входит — передаётся через stdin):

    sudo bash -c 'cd /opt/homelab-dashboard && docker compose exec -T dashboard python - < tools/netcheck.py'

Проверяет то, что нужно дашборду в работе: TCP до устройств локальной сети (SSH-сбор, проверки
сервисов), DNS (только для сервисов, заданных именем) и, для справки, выход в интернет.
"""
import socket

TARGETS = {
    "роутер MikroTik 192.168.1.1:80 (webfig)": ("192.168.1.1", 80),
    "роутер MikroTik 192.168.1.1:22": ("192.168.1.1", 22),
    "Raspberry Pi 192.168.1.10:22": ("192.168.1.10", 22),
    "этот сервер 192.168.1.39:22": ("192.168.1.39", 22),
    "сервер через host.docker.internal:22": ("host.docker.internal", 22),
    "интернет 1.1.1.1:443 (не нужен дашборду)": ("1.1.1.1", 443),
}

for name, (host, port) in TARGETS.items():
    try:
        socket.create_connection((host, port), timeout=3).close()
        print("OK   ", name)
    except OSError as e:
        print("FAIL ", name, "—", e.__class__.__name__, e)

for host in ("pypi.org", "host.docker.internal"):
    try:
        print("OK    DNS", host, "→", socket.gethostbyname(host))
    except OSError as e:
        print("FAIL  DNS", host, "—", e)
