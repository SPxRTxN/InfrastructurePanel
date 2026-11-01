"""Температуры: /sys/class/thermal/thermal_zone* и /sys/class/hwmon/hwmon*.

На Raspberry Pi зона `cpu-thermal` даёт `thermal.cpu.temp`; тот же датчик,
продублированный в hwmon (`cpu_thermal`), второй раз не публикуется.
"""
from __future__ import annotations

import glob
import os

from .base import Collector, Sensor, norm_name, register

# Типы зон, которые означают температуру процессора
CPU_ZONE_TYPES = {"cpu_thermal", "cpu", "x86_pkg_temp", "soc_thermal", "cpu0_thermal"}


@register("thermal")
class ThermalCollector(Collector):
    def __init__(self, cfg=None, default_interval=10):
        super().__init__(cfg, default_interval)
        self._map: dict[str, tuple[str, str]] | None = None  # id → (путь, подпись)

    def _discover(self) -> dict[str, tuple[str, str]]:
        found: dict[str, tuple[str, str]] = {}
        zone_types: set[str] = set()

        def add(name: str, path: str, label: str):
            sid, n = f"thermal.{name}.temp", 2
            while sid in found:
                sid, n = f"thermal.{name}_{n}.temp", n + 1
            found[sid] = (path, label)

        for zone in sorted(glob.glob(f"{self.sys}/class/thermal/thermal_zone*")):
            ztype = (self._read_text(f"{zone}/type") or "").strip()
            if not ztype or not os.path.exists(f"{zone}/temp"):
                continue
            key = norm_name(ztype)
            zone_types.add(key)
            add("cpu" if key in CPU_ZONE_TYPES else key, f"{zone}/temp", ztype)

        for hw in sorted(glob.glob(f"{self.sys}/class/hwmon/hwmon*")):
            hname = norm_name(self._read_text(f"{hw}/name") or os.path.basename(hw))
            if hname in zone_types:
                continue  # дубль thermal_zone
            inputs = sorted(glob.glob(f"{hw}/temp*_input"))
            for inp in inputs:
                base = inp[: -len("_input")]
                lbl = (self._read_text(f"{base}_label") or "").strip()
                if len(inputs) == 1 and not lbl:
                    name = hname
                else:
                    name = f"{hname}_{norm_name(lbl or os.path.basename(base))}"
                add(name, inp, f"{hname} {lbl}".strip())
        return found

    def _sensors_map(self):
        if self._map is None:
            self._map = self._discover()
        return self._map

    def sensors(self):
        return [Sensor(sid, "temperature", label, "°C") for sid, (_p, label) in self._sensors_map().items()]

    def read(self):
        values = {}
        for sid, (path, _label) in self._sensors_map().items():
            raw = (self._read_text(path) or "").strip()
            try:
                values[sid] = round(int(raw) / 1000, 1)  # миллиградусы
            except ValueError:
                continue
        return values
