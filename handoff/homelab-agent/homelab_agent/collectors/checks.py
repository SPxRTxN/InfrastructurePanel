"""Проверки доступности: TCP-подключение и HTTP-статус.

    [collectors.checks]
    tcp = [ { name = "router", host = "192.168.1.1", port = 80 } ]
    http = [ { name = "dashboard", url = "http://127.0.0.1:8000/health", expect = 200 } ]

Датчики: `checks.<name>.up` (1/0) и `checks.<name>.latency_ms` (только если up).
"""
from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request

from .base import Collector, Sensor, check_items, register


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Ожидаемый статус сверяется с первым ответом, без перехода по редиректам
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


@register("checks")
class ChecksCollector(Collector):
    KEYS = {"tcp", "http"}

    def __init__(self, cfg=None, default_interval=10):
        super().__init__(cfg, default_interval)
        self.tcp = check_items("checks", self.cfg.get("tcp", []),
                               {"name": str, "host": str, "port": int}, {"timeout": (int, float)})
        self.http = check_items("checks", self.cfg.get("http", []),
                                {"name": str, "url": str}, {"expect": int, "timeout": (int, float)})
        names = [c["name"] for c in self.tcp + self.http]
        if len(names) != len(set(names)):
            raise ValueError("[collectors.checks]: имена tcp и http проверок должны быть разными")
        for c in self.tcp:
            if not 1 <= c["port"] <= 65535:
                raise ValueError(f"[collectors.checks] {c['name']}: port 1..65535")
        for c in self.http:
            if not c["url"].startswith(("http://", "https://")):
                raise ValueError(f"[collectors.checks] {c['name']}: url должен начинаться с http:// или https://")

    def sensors(self):
        out = []
        for c in self.tcp + self.http:
            n = c["name"]
            out += [Sensor(f"checks.{n}.up", "state", n), Sensor(f"checks.{n}.latency_ms", "latency", n, "ms")]
        return out

    @staticmethod
    def _tcp(host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _http(url: str, expect: int, timeout: float) -> bool:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "homelab-agent"})
        try:
            with _opener.open(req, timeout=timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (OSError, ValueError):
            return False
        return status == expect

    def read(self):
        values = {}
        jobs = [(c, lambda c=c: self._tcp(c["host"], c["port"], float(c.get("timeout", 3)))) for c in self.tcp]
        jobs += [(c, lambda c=c: self._http(c["url"], c.get("expect", 200), float(c.get("timeout", 5))))
                 for c in self.http]
        for c, job in jobs:
            started = time.monotonic()
            up = job()
            values[f"checks.{c['name']}.up"] = 1 if up else 0
            if up:
                values[f"checks.{c['name']}.latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        return values
