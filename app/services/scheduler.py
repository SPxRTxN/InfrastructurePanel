"""Простой фоновый планировщик на asyncio: без внешних зависимостей и отдельных процессов.

Задача = (имя, функция интервала в секундах, корутина). Тик раз в 5 секунд;
задача запускается, когда с прошлого запуска прошло не меньше её интервала.
"""
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger("scheduler")

TICK = 5
JOBS: list[tuple[str, Callable[[], int], Callable[[], Awaitable[None]]]] = []


def job(name: str, interval: Callable[[], int]):
    def deco(fn):
        JOBS.append((name, interval, fn))
        return fn

    return deco


async def _loop() -> None:
    last: dict[str, float] = {}
    while True:
        now = time.monotonic()
        for name, interval, fn in JOBS:
            if name not in last or now - last[name] >= interval():
                last[name] = now
                try:
                    await fn()
                except Exception:  # одна упавшая задача не должна останавливать остальные
                    log.exception("Ошибка фоновой задачи %s", name)
        await asyncio.sleep(TICK)


def start() -> asyncio.Task:
    return asyncio.create_task(_loop())
