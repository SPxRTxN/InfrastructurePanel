"""Сведения об узле для hello и экрана network: ОС, ядро, CPU, IP и MAC основного интерфейса."""
from __future__ import annotations

import os
import platform
import socket

import psutil

# Номер ядра ARM из /proc/cpuinfo («CPU part») → имя; на Pi модель ядра иначе не узнать
ARM_PARTS = {"0xd03": "Cortex-A53", "0xd08": "Cortex-A72", "0xd0b": "Cortex-A76", "0xd05": "Cortex-A55"}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def default_interface(proc: str = "/proc") -> str | None:
    """Интерфейс маршрута по умолчанию (destination 00000000) из /proc/net/route."""
    for line in _read(f"{proc}/net/route").splitlines()[1:]:
        parts = line.split()
        if len(parts) > 3 and parts[1] == "00000000" and int(parts[3], 16) & 2:  # флаг RTF_GATEWAY
            return parts[0]
    return None


def os_name(etc: str = "/etc") -> str:
    for line in _read(f"{etc}/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    if platform.system() == "Windows":
        return f"Windows {platform.release()} ({platform.version()})"
    return platform.system()


def cpu_model(proc: str = "/proc") -> str:
    text = _read(f"{proc}/cpuinfo")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        fields.setdefault(key.strip(), val.strip())
    if fields.get("model name"):
        return fields["model name"]
    part = fields.get("CPU part", "").lower()
    if part in ARM_PARTS:
        return ARM_PARTS[part]
    return platform.processor() or platform.machine()


def network(proc: str = "/proc") -> tuple[str, list[str], str | None]:
    """(MAC, [IPv4 основного интерфейса первыми], имя интерфейса)."""
    try:
        addrs = psutil.net_if_addrs()
    except OSError:
        return "", [], None
    iface = default_interface(proc)
    order = ([iface] if iface in addrs else []) + sorted(n for n in addrs if n != iface and n != "lo")
    mac, ips = "", []
    for name in order:
        for a in addrs[name]:
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                ips.append(a.address)
            elif a.family == psutil.AF_LINK and not mac and name == order[0]:
                mac = a.address
    return mac, ips[:4], iface


def collect(proc: str = "/proc") -> dict:
    mac, ips, _iface = network(proc)
    mem_kb = 0
    for line in _read(f"{proc}/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            mem_kb = int(line.split()[1])
            break
    if not mem_kb:  # нет /proc (Windows, BSD)
        mem_kb = psutil.virtual_memory().total // 1024
    try:
        boot_ts = int(psutil.boot_time() * 1000)
    except OSError:
        boot_ts = 0
    return {
        "mac": mac,
        "ip": ips,
        "info": {
            "os": os_name(),
            "kernel": platform.release(),
            "hostname": socket.gethostname(),
            "cpu": cpu_model(proc),
            "threads": os.cpu_count() or 1,
            "memory_kb": mem_kb,
            "boot_ts": boot_ts,
        },
    }
