"""Время работы узла (из /proc/uptime; без /proc — psutil.boot_time)."""
from __future__ import annotations

import time

import psutil

from .base import Collector, Sensor, register


@register("uptime")
class UptimeCollector(Collector):
    def sensors(self):
        return [Sensor("uptime.seconds", "duration", "Время работы", "s")]

    def read(self):
        if self.use_psutil:
            return {"uptime.seconds": int(time.time() - psutil.boot_time())}
        text = self._read_text(f"{self.proc}/uptime")
        if not text:
            return {}
        return {"uptime.seconds": int(float(text.split()[0]))}
