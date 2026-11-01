"""Настройки узла из панели: `config_get` / `config_set` и удаление агента по команде панели.

Принцип: панель может менять только «безопасные» разделы (MANAGED) и только если владелец не выключил это
на узле (`[remote] config = false`). Всё, что что-то запускает или даёт доступ (panel, actions, vpn,
collectors.command, update.restart_argv), меняется только на узле руками владельца. Присланное сохраняется
как данные в etc/panel.toml и проверяется тем же разбором, что и agent.toml; ничего из него не выполняется.

Загрузка: agent.toml, поверх — разделы из panel.toml (раздел целиком заменяет одноимённый раздел agent.toml;
исключение — collectors.command, он всегда остаётся из agent.toml).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import tomllib

from . import guard

log = logging.getLogger(__name__)

MANAGED = ("interval", "collectors", "display", "wol", "update")
LOCAL_ONLY = ("panel", "actions", "vpn", "collectors.command")
# Ключи внутри managed-разделов, которые тоже только на узле: update.restart_argv — это команда для запуска
FORBIDDEN_KEYS = {"update": {"restart_argv"}, "collectors": {"command"}}
REMOTE_KEYS = {"config", "uninstall"}
MAX_BYTES = 64 * 1024
PANEL_FILE = "panel.toml"


class RemoteError(ValueError):
    """Отказ с текстом для панели (уходит в detail)."""


# ---------- Пути ----------

def panel_path(config_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), PANEL_FILE)


def layout_ok(config_path: str | None) -> bool:
    """Агент установлен новой раскладкой: конфиг лежит в <папка>/etc (а не в /etc/homelab-agent)."""
    return bool(config_path) and os.path.dirname(os.path.abspath(config_path)) == os.path.abspath(guard.ETC_DIR)


# ---------- Разбор [remote] ----------

def parse_remote(raw: dict | None) -> dict:
    raw = dict(raw or {})
    unknown = set(raw) - REMOTE_KEYS
    if unknown:
        raise ValueError(f"[remote]: неизвестные ключи: {', '.join(sorted(unknown))}")
    out = {}
    for key in REMOTE_KEYS:
        val = raw.get(key, True)
        if not isinstance(val, bool):
            raise ValueError(f"[remote]: {key} — true/false")
        out[key] = val
    return out


# ---------- Слияние при загрузке ----------

def _strip_forbidden(name: str, value):
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k not in FORBIDDEN_KEYS.get(name, ())}
    return value


def read_panel_toml(path: str) -> dict:
    """Managed-разделы из panel.toml. Нет файла или он испорчен — пусто (агент не должен падать из-за него)."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("%s не прочитан, игнорирую: %s", path, exc)
        return {}
    ignored = sorted(set(data) - set(MANAGED))
    if ignored:
        log.warning("%s: разделы вне managed игнорируются: %s", path, ", ".join(ignored))
    return {k: _strip_forbidden(k, v) for k, v in data.items() if k in MANAGED}


def merge(base: dict, over: dict) -> dict:
    """agent.toml + managed-разделы из panel.toml (раздел заменяется целиком, collectors.command остаётся)."""
    out = dict(base)
    for name, value in over.items():
        if name not in MANAGED:
            continue
        if name == "collectors" and isinstance(value, dict):
            value = dict(value)
            cmd = (base.get("collectors") or {}).get("command")
            if cmd is not None:
                value["command"] = cmd
        out[name] = value
    return out


def validate(cfg) -> None:
    """Глубже parse_config: те же проверки, что у `--check-config`, но без файлов токенов. ValueError — причина."""
    from .collectors import build_collectors
    from .updater import parse_update
    from .wol import parse_wol

    build_collectors(cfg.collectors, cfg.interval)
    parse_wol(cfg.wol)
    parse_update(cfg.update)
    if cfg.display.get("enabled"):
        from .display.base import parse_display

        parse_display(cfg.display)


# ---------- Запись TOML ----------

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE.match(k) else json.dumps(k, ensure_ascii=False)


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise RemoteError("число вне допустимого диапазона")
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # экранирование JSON совпадает с базовыми строками TOML
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(str(k))} = {_value(x)}" for k, x in v.items()) + " }" if v else "{}"
    raise RemoteError(f"значение типа {type(v).__name__} не сохранить в TOML")


def dump_toml(data: dict) -> str:
    """Минимальная запись TOML для словарей из скаляров, списков и таблиц (без зависимостей)."""
    lines: list[str] = []

    def table(prefix: str, d: dict) -> None:
        scalars = [(k, v) for k, v in d.items() if not isinstance(v, dict)]
        tables = [(k, v) for k, v in d.items() if isinstance(v, dict)]
        if prefix and (scalars or not tables):
            lines.append(f"[{prefix}]")
        for k, v in scalars:
            lines.append(f"{_key(str(k))} = {_value(v)}")
        if scalars or (prefix and not tables):
            lines.append("")
        for k, v in tables:
            table(f"{prefix}.{_key(str(k))}" if prefix else _key(str(k)), v)

    table("", data)
    text = "\n".join(lines).strip() + "\n"
    if tomllib.loads(text) != data:  # страховка: записанное обязано читаться обратно тем же
        raise RemoteError("значения не удалось надёжно записать в TOML")
    return text


def write_atomic(path: str, text: str) -> None:
    """Временный файл 0600 рядом и os.replace: при сбое остаётся прежний panel.toml целиком."""
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- config_get / config_set ----------

def current_sections(cfg) -> dict:
    """Текущие значения managed-разделов (без запрещённых ключей) — ответ на config_get."""
    from .collectors.base import DEFAULT_COLLECTORS

    collectors = cfg.collectors if cfg.collectors is not None else {n: {} for n in DEFAULT_COLLECTORS}
    return {
        "interval": cfg.interval if cfg.interval != int(cfg.interval) else int(cfg.interval),
        "collectors": _strip_forbidden("collectors", collectors),
        "display": cfg.display,
        "wol": cfg.wol,
        "update": _strip_forbidden("update", cfg.update),
    }


def config_message(uid: str, cfg) -> dict:
    return {"type": "config", "id": uid, "managed": list(MANAGED), "sections": current_sections(cfg),
            "local_only": list(LOCAL_ONLY)}


def check_sections(sections) -> dict:
    """Входной контроль присланного: только managed, без запрещённых ключей и без null."""
    if not isinstance(sections, dict) or not sections:
        raise RemoteError("sections — непустой объект")
    for name, value in sections.items():
        if name not in MANAGED:
            raise RemoteError(f"раздел «{name}» меняется только на узле")
        if name == "interval":
            continue
        if not isinstance(value, dict):
            raise RemoteError(f"[{name}] должна быть таблицей")
        bad = FORBIDDEN_KEYS.get(name, set()) & set(value)
        if bad:
            raise RemoteError(f"{name}.{sorted(bad)[0]} меняется только на узле")
    return sections


def apply_config_set(config_path: str | None, sections) -> str:
    """Проверить и записать panel.toml. Возвращает текст для detail; RemoteError — отказ (файл не тронут)."""
    from .config import ConfigError, parse_config

    if not config_path:
        raise RemoteError("неизвестен путь конфига")
    check_sections(sections)
    try:
        with open(config_path, "rb") as f:
            base = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RemoteError(f"agent.toml не прочитан: {getattr(exc, 'strerror', None) or exc}") from None
    target = panel_path(config_path)
    over = read_panel_toml(target)
    over.update(sections)  # присланные разделы заменяют целиком, остальные остаются как были
    try:
        # Итоговый конфиг обязан пройти тот же разбор, что и при старте
        cfg = parse_config(merge(base, over), config_path)
        validate(cfg)
        text = dump_toml(over)
    except (ConfigError, ValueError, TypeError) as exc:
        raise RemoteError(f"конфиг не принят: {exc}"[:400]) from None
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise RemoteError("слишком большой конфиг")
    try:
        write_atomic(target, text)
    except OSError as exc:
        raise RemoteError(f"не удалось записать {PANEL_FILE}: {exc.strerror}") from None
    return "настройки сохранены, агент перезапускается"


# ---------- Удаление по команде ----------

def launch_uninstall(script: str | None = None, run=None) -> int:
    """Запустить фиксированный root-скрипт удаления отдельным процессом. Аргументы — только константы, не из сообщения."""
    argv = ["sudo", "-n", script or guard.UNINSTALL_SCRIPT, "--from-panel"]
    if run is not None:
        return run(argv)
    try:
        # Отделяем от службы (новая сессия): скрипт сам уходит в systemd-run, но sudo не должен умереть вместе с нами
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return 127
    return 0
