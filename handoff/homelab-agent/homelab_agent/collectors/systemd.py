"""Состояние юнитов systemd: `systemd.<unit>.active` = 1/0 (`systemctl is-active`).

    [collectors.systemd]
    units = ["nginx.service", "docker.service"]
"""
from __future__ import annotations

import re

from .base import Collector, Sensor, register, run_argv

UNIT_RE = re.compile(r"^[A-Za-z0-9@._:\\-]{1,128}\.(service|socket|timer|mount|path|target|slice|scope)$")
ACTIVE = {"active", "reloading"}  # reloading — сервис работает, перечитывает конфиг


@register("systemd")
class SystemdCollector(Collector):
    KEYS = {"units", "timeout"}
    run_argv = staticmethod(run_argv)  # подменяется в тестах

    def __init__(self, cfg=None, default_interval=10):
        super().__init__(cfg, default_interval)
        units = self.cfg.get("units", [])
        if not isinstance(units, list) or not all(isinstance(u, str) and UNIT_RE.match(u) for u in units):
            raise ValueError('[collectors.systemd]: units — список имён юнитов с суффиксом, например "nginx.service"')
        self.units = units
        self.timeout = float(self.cfg.get("timeout", 10))

    def sensors(self):
        return [Sensor(f"systemd.{u}.active", "state", u) for u in self.units]

    def read(self):
        if not self.units:
            return {}
        # Код возврата ≠ 0, если хоть один юнит не active, — это нормально
        _code, out, _err = self.run_argv(["systemctl", "is-active", "--", *self.units], self.timeout)
        lines = out.split()
        if len(lines) != len(self.units):
            return {}
        return {f"systemd.{u}.active": 1 if state in ACTIVE else 0 for u, state in zip(self.units, lines)}
