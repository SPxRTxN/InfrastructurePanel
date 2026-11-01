"""RAM: всего / занято / доступно в байтах (из /proc/meminfo, как `free -b`; без /proc — psutil)."""
from __future__ import annotations

import psutil

from .base import Collector, Sensor, register


def parse_meminfo(text: str) -> dict[str, int]:
    """'MemTotal:  948000 kB' → {'MemTotal': 970752000} (байты)."""
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts or not parts[0].isdigit():
            continue
        mult = 1024 if len(parts) > 1 and parts[1] == "kB" else 1
        out[key.strip()] = int(parts[0]) * mult
    return out


@register("memory")
class MemoryCollector(Collector):
    def sensors(self):
        return [
            Sensor("memory.total", "memory", "RAM всего", "B"),
            Sensor("memory.used", "memory", "RAM занято", "B"),
            Sensor("memory.available", "memory", "RAM доступно", "B"),
        ]

    def read(self):
        if self.use_psutil:
            vm = psutil.virtual_memory()
            return {"memory.total": vm.total, "memory.used": max(vm.total - vm.available, 0),
                    "memory.available": vm.available}
        text = self._read_text(f"{self.proc}/meminfo")
        if not text:
            return {}
        info = parse_meminfo(text)
        total = info.get("MemTotal")
        if not total:
            return {}
        avail = info.get("MemAvailable")
        if avail is None:  # очень старые ядра: оценка как в procps
            avail = info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0)
        return {
            "memory.total": total,
            # «Занято» как в современном `free`: всё, что недоступно приложениям
            "memory.used": max(total - avail, 0),
            "memory.available": avail,
        }
