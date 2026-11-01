"""Действия восстановления: только allowlist из `[actions.*]` конфига узла.

Панель присылает лишь имя действия; команда и аргументы берутся из конфига.
Запуск — списком аргументов без shell, с таймаутом, лимитом частоты,
одним действием одновременно и обрезкой вывода до 4 КБ.

    [actions.restart_nginx]
    label = "Перезапустить nginx"
    argv = ["sudo", "-n", "systemctl", "restart", "nginx.service"]
    timeout = 30
    limit = "3/h"
    auto = { when = "systemd.nginx.service.active == 0", for = 60 }
"""
from __future__ import annotations

import asyncio
import logging
import operator
import os
import re
import signal
import time
from collections import deque
from dataclasses import dataclass, field

from . import protocol
from .collectors.base import SAFE_ENV

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9_]{1,48}$")
LIMIT_RE = re.compile(r"^\s*(\d+)\s*/\s*(s|m|h|d)\s*$")
WINDOWS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
KEYS = {"argv", "timeout", "limit", "label", "auto"}
OPS = {"==": operator.eq, "!=": operator.ne, "<=": operator.le, ">=": operator.ge, "<": operator.lt, ">": operator.gt}
WHEN_RE = re.compile(r"^\s*([A-Za-z0-9_./:@-]+)\s*(==|!=|<=|>=|<|>)\s*(-?\d+(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class AutoRule:
    when: str
    sensor: str
    op: str
    value: float
    hold: float  # секунд условие должно держаться

    def holds(self, values: dict) -> bool | None:
        """True/False; None — датчика нет (условие не оценивается)."""
        if self.sensor not in values:
            return None
        return OPS[self.op](values[self.sensor], self.value)


@dataclass(frozen=True)
class ActionSpec:
    name: str
    argv: tuple[str, ...]
    label: str
    timeout: float = 30
    limit_count: int = 3
    limit_window: int = 3600
    auto: AutoRule | None = field(default=None)


def _parse_auto(name: str, raw) -> AutoRule:
    where = f"[actions.{name}].auto"
    if not isinstance(raw, dict) or set(raw) - {"when", "for"} or "when" not in raw:
        raise ValueError(f'{where}: ожидается {{ when = "<датчик> <оп> <число>", for = <секунд> }}')
    m = WHEN_RE.match(str(raw["when"]))
    if not m:
        raise ValueError(f"{where}: when вида \"systemd.nginx.service.active == 0\" (операции: {' '.join(OPS)})")
    hold = raw.get("for", 60)
    if not isinstance(hold, (int, float)) or hold < 0:
        raise ValueError(f"{where}: for — секунды ≥ 0")
    return AutoRule(raw["when"], m.group(1), m.group(2), float(m.group(3)), float(hold))


def parse_actions(section: dict | None) -> dict[str, ActionSpec]:
    specs = {}
    for name, raw in (section or {}).items():
        where = f"[actions.{name}]"
        if not NAME_RE.match(name):
            raise ValueError(f"{where}: имя — строчные латиница, цифры и _")
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: должна быть таблицей")
        unknown = set(raw) - KEYS
        if unknown:
            raise ValueError(f"{where}: неизвестные ключи: {', '.join(sorted(unknown))}")
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise ValueError(f"{where}: argv — непустой список строк (команда без shell)")
        timeout = raw.get("timeout", 30)
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            raise ValueError(f"{where}: timeout — секунды 0..3600")
        m = LIMIT_RE.match(str(raw.get("limit", "3/h")))
        if not m or int(m.group(1)) < 1:
            raise ValueError(f'{where}: limit вида "3/h" (единицы s, m, h, d)')
        specs[name] = ActionSpec(
            name=name,
            argv=tuple(argv),
            label=str(raw.get("label") or name)[:80],
            timeout=float(timeout),
            limit_count=int(m.group(1)),
            limit_window=WINDOWS[m.group(2)],
            auto=_parse_auto(name, raw["auto"]) if "auto" in raw else None,
        )
    return specs


async def _drain(stream, limit: int) -> bytes:
    """Читает поток до конца, но хранит только первые `limit` байт (не держим мегабайты в памяти)."""
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return bytes(buf)
        if len(buf) <= limit:
            buf += chunk[: limit + 1 - len(buf)]


class ActionRunner:
    def __init__(self, specs: dict[str, ActionSpec], clock=time.monotonic):
        self.specs = specs
        self.clock = clock
        self._lock = asyncio.Lock()
        self._history: dict[str, deque[float]] = {n: deque() for n in specs}
        self.last: dict | None = None  # последнее действие — для экрана дисплея

    def describe(self) -> list[dict]:
        return [{"name": s.name, "label": s.label} for s in self.specs.values()]

    def _rate_ok(self, spec: ActionSpec, now: float) -> bool:
        hist = self._history[spec.name]
        while hist and now - hist[0] >= spec.limit_window:
            hist.popleft()
        return len(hist) < spec.limit_count

    async def run(self, action_id: str, name: str, source: str = "panel") -> dict:
        spec = self.specs.get(name)
        if spec is None:
            log.warning("действие %r (%s): неизвестно, отклонено", name[:64], source)
            return protocol.action_result(action_id, name, False, None, error="unknown action")
        if self._lock.locked():
            log.warning("действие %s (%s): занято другим действием", name, source)
            return protocol.action_result(action_id, name, False, None, error="busy")
        if not self._rate_ok(spec, self.clock()):
            log.warning("действие %s (%s): превышен лимит %d за %d с", name, source, spec.limit_count,
                        spec.limit_window)
            return protocol.action_result(action_id, name, False, None, error="rate limited")
        async with self._lock:
            self._history[name].append(self.clock())
            result = await self._execute(action_id, spec)
        self.last = {"name": name, "ok": result["ok"], "ts": time.time(), "source": source}
        log.info("действие %s (%s): ok=%s код=%s %d мс%s", name, source, result["ok"], result["code"],
                 result["duration_ms"], f" ошибка={result['error']}" if result.get("error") else "")
        if result["output"]:
            log.info("действие %s, вывод:\n%s", name, result["output"])
        return result

    async def _execute(self, action_id: str, spec: ActionSpec) -> dict:
        started = time.monotonic()

        def took() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=SAFE_ENV,
                start_new_session=True,  # своя группа процессов — по таймауту убиваем всех потомков
            )
        except OSError as exc:
            return protocol.action_result(action_id, spec.name, False, None, f"не запустить: {exc.strerror}",
                                          took())
        reader = asyncio.create_task(_drain(proc.stdout, protocol.MAX_OUTPUT))
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=spec.timeout)
        except asyncio.TimeoutError:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGKILL)
                else:  # Windows: групп процессов POSIX нет
                    proc.kill()
            except ProcessLookupError:
                pass
            except PermissionError:
                # В группе есть процессы root (через sudo) — их убить нельзя, убиваем свой
                proc.kill()
            await proc.wait()
            reader.cancel()
            return protocol.action_result(action_id, spec.name, False, None, "", took(), error="timeout")
        output = protocol.truncate_output(await reader)
        return protocol.action_result(action_id, spec.name, code == 0, code, output, took())


class AutoRecovery:
    """Следит за правилами `auto`: условие держится `for` секунд → запуск действия."""

    def __init__(self, specs: dict[str, ActionSpec], clock=time.monotonic):
        self.rules = {n: s.auto for n, s in specs.items() if s.auto}
        self.clock = clock
        self._since: dict[str, float] = {}

    def due(self, values: dict) -> list[str]:
        now = self.clock()
        out = []
        for name, rule in self.rules.items():
            if rule.holds(values):
                since = self._since.setdefault(name, now)
                if now - since >= rule.hold:
                    out.append(name)
                    self._since.pop(name)  # отсчёт заново: следующий запуск не раньше ещё одного `for`
            else:
                self._since.pop(name, None)
        return out
