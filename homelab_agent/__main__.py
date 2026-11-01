"""Точка входа: python -m homelab_agent [-c agent.toml] [--once | --check-config]."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time

from . import __version__
from .collectors import build_collectors, safe_read
from .config import ConfigError, default_path, check_config, load_config

log = logging.getLogger("homelab_agent")


def collect_once(collectors, pause: float = 1.0) -> dict:
    """Два прохода с паузой: у CPU и процессов значения — разница между чтениями."""
    values: dict = {}
    for c in collectors:
        values.update(safe_read(c))
    if pause:
        time.sleep(pause)
        for c in collectors:
            values.update(safe_read(c))
    return values


def cmd_once(cfg) -> int:
    collectors = build_collectors(cfg.collectors, cfg.interval)
    sensors = [s.as_dict() for c in collectors for s in c.sensors()]
    out = {
        "node": cfg.node,
        "ts": int(time.time() * 1000),
        "sensors": sensors,
        "values": collect_once(collectors),
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_check(cfg) -> int:
    problems = check_config(cfg)
    if problems:
        for p in problems:
            print(f"ОШИБКА: {p}")
        return 1
    print(f"OK: узел {cfg.node}, панелей {len(cfg.panels)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="homelab_agent", description="Агент мониторинга homelab")
    ap.add_argument("-c", "--config", default=os.environ.get("HOMELAB_AGENT_CONFIG") or default_path())
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="один сбор, JSON в stdout")
    mode.add_argument("--check-config", action="store_true", help="проверить конфиг и права на токены")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log-file", help="журнал в файл с ротацией (Windows: pythonw без консоли)")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    if args.log_file:
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(args.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=[handler])
    else:
        # Время проставляет journald, поэтому в формате его нет
        logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    try:
        cfg = load_config(args.config)
        if args.once:
            return cmd_once(cfg)
        if args.check_config:
            return cmd_check(cfg)
        return cmd_run(cfg)
    except (ConfigError, ValueError) as exc:
        print(f"ОШИБКА конфига: {exc}", file=sys.stderr)
        return 2


def cmd_run(cfg) -> int:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Права root агенту не нужны; перезапуск сервисов — через sudoers на конкретные команды
        print("ОШИБКА: агент не запускается под root (см. packaging/sudoers.example)", file=sys.stderr)
        return 2
    from .runner import Agent

    agent = Agent(cfg)

    async def amain():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows: остановку делает планировщик задач, Ctrl+C — KeyboardInterrupt
                pass
        await agent.run(stop)

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
    log.info("агент остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
