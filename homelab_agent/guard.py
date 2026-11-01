"""Сторож обновления: вернуть прежнюю версию, если новая не подключилась к панели.

Работает отдельно от агента — root-таймером systemd (`homelab-agent-guard.timer`, раз в минуту) — и поэтому
**только стандартная библиотека и ни одного импорта из пакета**: install.sh кладёт копию этого файла в
/opt/homelab-agent/bin/guard.py (владелец root, агенту недоступна), чтобы сторож не зависел от версии, которая,
возможно, сломана.

Обмен через каталог состояния (/opt/homelab-agent/state):
  switch.json   пишет агент при переключении `current`: {version, previous, ts, confirm_seconds}
  connected     пишет транспорт агента после welcome от панели: «<версия> <unix-время>»
  rollback.json пишет сторож при откате: {from, to, ts}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SWITCH, MARK, ROLLBACK = "switch.json", "connected", "rollback.json"
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$")
# Раскладка узла — единственное место, где записаны пути (остальные модули берут их отсюда)
ROOT = "/opt/homelab-agent"
BIN_DIR, ETC_DIR, APP_DIR, STATE_DIR = f"{ROOT}/bin", f"{ROOT}/etc", f"{ROOT}/app", f"{ROOT}/state"
UNINSTALL_SCRIPT = f"{BIN_DIR}/uninstall-agent"
LEGACY_ETC_DIR = "/etc/homelab-agent"
DEFAULT_PREFIX = APP_DIR  # здесь лежат releases/, current, previous


def state_dir() -> Path:
    """Каталог состояния: HOMELAB_AGENT_STATE, затем STATE_DIRECTORY от systemd, затем STATE_DIR."""
    env = os.environ.get("HOMELAB_AGENT_STATE") or os.environ.get("STATE_DIRECTORY", "").split(":")[0]
    return Path(env or STATE_DIR)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_mark(version: str, directory: Path | None = None, now: float | None = None) -> None:
    """Отметка «подключился к панели». Каталога нет (разработка, Windows) или не пишется — молча пропускаем."""
    d = directory or state_dir()
    try:
        if d.is_dir():
            _write_atomic(d / MARK, f"{version} {int(now if now is not None else time.time())}\n")
    except OSError:
        pass


def read_mark(d: Path) -> tuple[str, float] | None:
    try:
        version, ts = (d / MARK).read_text(encoding="utf-8").split()
        return version, float(ts)
    except (OSError, ValueError):
        return None


def write_switch(d: Path, version: str, previous: str, confirm_seconds: int, now: float | None = None) -> None:
    data = {"version": version, "previous": previous, "ts": now if now is not None else time.time(),
            "confirm_seconds": confirm_seconds}
    _write_atomic(d / SWITCH, json.dumps(data))


def replace_symlink(link: Path, target: str) -> None:
    """Атомарно направить link на target (относительный путь): создаём рядом и переименовываем поверх."""
    tmp = link.with_name(link.name + ".new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)


def _target(prefix: Path, name: str) -> str | None:
    """Имя версии, на которую указывает symlink prefix/name, или None."""
    try:
        t = (prefix / name).resolve(strict=True)
    except OSError:
        return None
    return t.name if t.parent == (prefix / "releases").resolve() else None


def check(prefix: Path, state: Path, now: float, restart) -> str:
    """Один проход сторожа. Возвращает: idle | confirmed | waiting | rolled_back | stale | error: …"""
    try:
        sw = json.loads((state / SWITCH).read_text(encoding="utf-8"))
        version, previous, ts, wait = sw["version"], sw["previous"], float(sw["ts"]), float(sw["confirm_seconds"])
    except FileNotFoundError:
        return "idle"
    except (OSError, ValueError, KeyError, TypeError):
        _drop(state)
        return "error: switch.json повреждён"
    if not (isinstance(version, str) and VERSION_RE.match(version) and isinstance(previous, str)
            and VERSION_RE.match(previous)):
        _drop(state)
        return "error: некорректные имена версий"
    if _target(prefix, "current") != version:
        _drop(state)  # кто-то уже переключил (install.sh, ручной откат) — сторожить нечего
        return "stale"
    mark = read_mark(state)
    if mark and mark[0] == version and mark[1] >= ts:
        _drop(state)
        return "confirmed"
    if now - ts < wait:
        return "waiting"
    if not (prefix / "releases" / previous / "homelab_agent").is_dir():
        _drop(state)
        return "error: прежней версии нет в releases/"
    replace_symlink(prefix / "current", f"releases/{previous}")
    _write_atomic(state / ROLLBACK, json.dumps({"from": version, "to": previous, "ts": now}))
    _drop(state)
    restart()
    return "rolled_back"


def _drop(state: Path) -> None:
    try:
        (state / SWITCH).unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    prefix = Path(args[0] if args else DEFAULT_PREFIX)
    status = check(prefix, state_dir(), time.time(),
                   lambda: subprocess.run(["systemctl", "restart", "homelab-agent.service"], timeout=60, check=False))
    if status != "idle":
        print(f"homelab-agent-guard: {status}")
    return 1 if status.startswith("error") else 0


if __name__ == "__main__":
    sys.exit(main())
