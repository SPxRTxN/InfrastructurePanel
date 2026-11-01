"""CPU: загрузка %, load average, частота (из /proc/stat, /proc/loadavg, cpufreq; без /proc — psutil)."""
from __future__ import annotations

import psutil

from .base import Collector, Sensor, register


@register("cpu")
class CpuCollector(Collector):
    def __init__(self, cfg=None, default_interval=10):
        super().__init__(cfg, default_interval)
        self._prev: tuple[int, int] | None = None  # (busy, total) с прошлого чтения

    @property
    def _freq_path(self) -> str:
        return f"{self.sys}/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"

    def sensors(self):
        out = [
            Sensor("cpu.percent", "load", "CPU", "%"),
            Sensor("cpu.load1", "load", "Load 1 мин"),
            Sensor("cpu.load5", "load", "Load 5 мин"),
            Sensor("cpu.load15", "load", "Load 15 мин"),
        ]
        if self._read_text(self._freq_path) is not None or (self.use_psutil and _psutil_freq() is not None):
            out.append(Sensor("cpu.freq_mhz", "frequency", "Частота CPU", "MHz"))
        return out

    def _read_psutil(self) -> dict[str, float]:
        # Первый вызов cpu_percent(None) — 0.0 (нет базы), дальше — за интервал между чтениями
        values = {"cpu.percent": psutil.cpu_percent(interval=None)}
        try:
            for key, val in zip(("cpu.load1", "cpu.load5", "cpu.load15"), psutil.getloadavg()):
                values[key] = round(val, 2)
        except (AttributeError, OSError):
            pass
        freq = _psutil_freq()
        if freq is not None:
            values["cpu.freq_mhz"] = round(freq, 1)
        return values

    def _times(self) -> tuple[int, int] | None:
        text = self._read_text(f"{self.proc}/stat")
        if not text:
            return None
        for line in text.splitlines():
            if line.startswith("cpu "):
                nums = [int(x) for x in line.split()[1:]]
                # user nice system idle iowait irq softirq steal (guest уже входит в user)
                nums = (nums + [0] * 8)[:8]
                idle = nums[3] + nums[4]
                total = sum(nums)
                return total - idle, total
        return None

    def read(self):
        if self.use_psutil:
            return self._read_psutil()
        values: dict[str, float] = {}
        cur = self._times()
        if cur:
            # Первое чтение — среднее с загрузки системы, дальше — за интервал
            busy0, total0 = self._prev or (0, 0)
            d_total = cur[1] - total0
            if d_total > 0:
                values["cpu.percent"] = round(100.0 * (cur[0] - busy0) / d_total, 1)
            self._prev = cur
        load = self._read_text(f"{self.proc}/loadavg")
        if load:
            parts = load.split()
            for key, part in zip(("cpu.load1", "cpu.load5", "cpu.load15"), parts[:3]):
                values[key] = float(part)
        freq = self._read_text(self._freq_path)
        if freq and freq.strip().isdigit():
            values["cpu.freq_mhz"] = round(int(freq.strip()) / 1000, 1)  # в файле кГц
        return values


def _psutil_freq() -> float | None:
    try:
        f = psutil.cpu_freq()
    except (AttributeError, OSError, NotImplementedError):
        return None
    return f.current if f and f.current else None
