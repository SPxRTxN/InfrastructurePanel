"""Свои метрики: команда из конфига узла, из вывода берётся число.

    [collectors.command]
    items = [ { name = "apt_updates", argv = ["/usr/lib/update-notifier/apt-check", "--quiet"],
                parse = "first_int", interval = 3600 } ]

parse: first_int | first_float — первое число в stdout (если там нет — в stderr);
       lines — число непустых строк stdout; exit_code — код возврата.
Команда запускается списком аргументов, без shell, с таймаутом (`timeout`, по умолчанию 10 с).
"""
from __future__ import annotations

import logging
import re
import subprocess
import time

from .base import Collector, Sensor, check_items, register, run_argv

log = logging.getLogger(__name__)

PARSERS = {"first_int", "first_float", "lines", "exit_code"}
_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+(?:\.\d+)?")


def parse_output(mode: str, code: int, out: str, err: str) -> float | None:
    if mode == "exit_code":
        return code
    if mode == "lines":
        return sum(1 for line in out.splitlines() if line.strip())
    rx = _INT if mode == "first_int" else _FLOAT
    for text in (out, err):
        m = rx.search(text)
        if m:
            return int(m.group()) if mode == "first_int" else float(m.group())
    return None


@register("command")
class CommandCollector(Collector):
    KEYS = {"items"}
    run_argv = staticmethod(run_argv)  # подменяется в тестах

    def __init__(self, cfg=None, default_interval=10):
        super().__init__(cfg, default_interval)
        self.items = check_items("command", self.cfg.get("items", []), {"name": str, "argv": list},
                                 {"parse": str, "interval": (int, float), "timeout": (int, float),
                                  "kind": str, "unit": str, "label": str})
        for it in self.items:
            if not it["argv"] or not all(isinstance(a, str) and a for a in it["argv"]):
                raise ValueError(f"[collectors.command] {it['name']}: argv — непустой список строк")
            if it.get("parse", "first_int") not in PARSERS:
                raise ValueError(f"[collectors.command] {it['name']}: parse — одно из {', '.join(sorted(PARSERS))}")
            Sensor("x", it.get("kind", "value"), "")  # проверка kind
        # Коллектор опрашивается с шагом самого частого элемента, каждый элемент — по своему интервалу
        intervals = [float(it.get("interval", self.interval)) for it in self.items]
        if intervals:
            self.interval = max(1.0, min(intervals))
        self._next: dict[str, float] = {}

    def sensors(self):
        return [Sensor(f"command.{it['name']}.value", it.get("kind", "value"), it.get("label", it["name"]),
                       it.get("unit", "")) for it in self.items]

    def read(self):
        values = {}
        now = time.monotonic()
        for it in self.items:
            name = it["name"]
            if self._next.get(name, 0) > now:
                continue
            self._next[name] = now + float(it.get("interval", self.interval))
            try:
                code, out, err = self.run_argv(it["argv"], float(it.get("timeout", 10)))
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.warning("command %s: %s", name, exc)
                continue
            val = parse_output(it.get("parse", "first_int"), code, out, err)
            if val is not None:
                values[f"command.{name}.value"] = val
        return values
