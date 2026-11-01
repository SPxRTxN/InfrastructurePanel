"""Процессы: запущен ли, сколько CPU % и RSS (байт) у процессов, чья командная строка содержит `match`.

    [collectors.processes]
    watch = [ { name = "nginx", match = "nginx: master" } ]

Если совпало несколько процессов — CPU и RSS суммируются.
"""
from __future__ import annotations

import os
import time

from .base import Collector, Sensor, check_items, register


@register("processes")
class ProcessesCollector(Collector):
    KEYS = {"watch"}

    def __init__(self, cfg=None, default_interval=10):
        super().__init__(cfg, default_interval)
        self.watch = check_items("processes", self.cfg.get("watch", []), {"name": str, "match": str}, {})
        self.clk_tck = os.sysconf("SC_CLK_TCK")
        self.page = os.sysconf("SC_PAGE_SIZE")
        self._prev: dict[str, tuple[float, dict[int, int]]] = {}  # name → (время, {pid: тики})

    def sensors(self):
        out = []
        for w in self.watch:
            n = w["name"]
            out += [
                Sensor(f"processes.{n}.running", "state", f"{n}: запущен"),
                Sensor(f"processes.{n}.cpu", "load", f"{n}: CPU", "%"),
                Sensor(f"processes.{n}.rss", "memory", f"{n}: RSS", "B"),
            ]
        return out

    def _scan(self) -> list[tuple[int, str, int, int]]:
        """[(pid, cmdline, тики CPU, rss байт)] по всем процессам, кроме самого агента."""
        me = os.getpid()
        out = []
        try:
            pids = [int(d) for d in os.listdir(self.proc) if d.isdigit()]
        except OSError:
            return out
        for pid in pids:
            if pid == me:
                continue
            cmd = self._read_text(f"{self.proc}/{pid}/cmdline")
            if not cmd:
                continue  # потоки ядра и исчезнувшие процессы
            stat = self._read_text(f"{self.proc}/{pid}/stat")
            statm = self._read_text(f"{self.proc}/{pid}/statm")
            if not stat or not statm:
                continue
            try:
                # comm в скобках может содержать пробелы — режем по последней «)»
                fields = stat.rsplit(")", 1)[1].split()
                ticks = int(fields[11]) + int(fields[12])  # utime + stime
                rss = int(statm.split()[1]) * self.page
            except (IndexError, ValueError):
                continue
            out.append((pid, cmd.replace("\0", " ").strip(), ticks, rss))
        return out

    def read(self):
        if not self.watch:
            return {}
        procs = self._scan()
        now = time.monotonic()
        values = {}
        for w in self.watch:
            n = w["name"]
            matched = [p for p in procs if w["match"] in p[1]]
            values[f"processes.{n}.running"] = 1 if matched else 0
            values[f"processes.{n}.rss"] = sum(p[3] for p in matched)
            ticks = {p[0]: p[2] for p in matched}
            prev = self._prev.get(n)
            if prev is not None and now > prev[0]:
                # Новый PID за интервал считается с нуля; исчезнувшие не учитываются
                delta = sum(max(t - prev[1].get(pid, 0), 0) for pid, t in ticks.items())
                values[f"processes.{n}.cpu"] = round(100.0 * delta / self.clk_tck / (now - prev[0]), 1)
            self._prev[n] = (now, ticks)
        return values
