"""Базовый класс коллектора и реестр.

Новый источник данных = один файл в этом пакете:

    @register("myname")
    class MyCollector(Collector):
        def sensors(self): ...
        def read(self): ...

Секция `[collectors.myname]` из TOML приходит в `__init__` как dict.
"""
from __future__ import annotations

import importlib
import logging
import math
import os
import pkgutil
import re
import time
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")

# Набор коллекторов, если в конфиге нет секции [collectors]
DEFAULT_COLLECTORS = ("cpu", "memory", "disks", "thermal", "uptime")

_REGISTRY: dict[str, type["Collector"]] = {}


@dataclass(frozen=True)
class Sensor:
    id: str
    kind: str
    label: str
    unit: str = ""

    def __post_init__(self):
        if not KIND_RE.match(self.kind):
            raise ValueError(f"недопустимый kind датчика {self.id}: {self.kind!r}")

    def as_dict(self) -> dict:
        return asdict(self)


def register(name: str):
    """Декоратор: регистрирует класс коллектора под именем секции конфига."""

    def deco(cls):
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            raise RuntimeError(f"коллектор {name!r} зарегистрирован дважды")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return deco


def registry() -> dict[str, type["Collector"]]:
    """Все коллекторы пакета (модули импортируются лениво, один раз)."""
    if not getattr(registry, "_loaded", False):
        pkg = importlib.import_module(__package__)
        for mod in pkgutil.iter_modules(pkg.__path__):
            if mod.name != "base":
                importlib.import_module(f"{__package__}.{mod.name}")
        registry._loaded = True
    return dict(_REGISTRY)


class Collector:
    """Источник датчиков. Наследник переопределяет `sensors()` и `read()`."""

    name = "base"
    # Ключи, которые понимает любой коллектор
    COMMON_KEYS = {"enabled", "interval", "proc_root", "sys_root"}
    # Ключи конкретного коллектора — переопределяется в наследнике
    KEYS: set[str] = set()

    def __init__(self, cfg: dict | None = None, default_interval: float = 10):
        cfg = dict(cfg or {})
        unknown = set(cfg) - self.COMMON_KEYS - self.KEYS
        if unknown:
            raise ValueError(f"[collectors.{self.name}]: неизвестные ключи: {', '.join(sorted(unknown))}")
        self.cfg = cfg
        self.interval = float(cfg.get("interval", default_interval))
        if not 1 <= self.interval <= 86400:
            raise ValueError(f"[collectors.{self.name}]: interval должен быть 1..86400 с")
        # Корни /proc и /sys можно переопределить: тесты и агент в контейнере с хостовым /proc
        self.proc = str(cfg.get("proc_root", "/proc")).rstrip("/")
        self.sys = str(cfg.get("sys_root", "/sys")).rstrip("/")
        # Нет /proc вовсе (Windows, BSD) — базовые коллекторы берут данные из psutil
        self.use_psutil = not os.path.isdir(self.proc)

    def sensors(self) -> list[Sensor]:
        raise NotImplementedError

    def read(self) -> dict[str, float]:
        raise NotImplementedError

    # --- помощники для наследников ---
    def _read_text(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return None


def clean_values(values: dict) -> dict[str, float]:
    """Оставляет только конечные числа (bool → 0/1); остальное отбрасывается."""
    out = {}
    for key, val in values.items():
        if isinstance(val, bool):
            val = int(val)
        if isinstance(val, (int, float)) and math.isfinite(val):
            out[str(key)] = val
    return out


def build_collectors(section: dict | None, default_interval: float) -> list[Collector]:
    """Создаёт коллекторы по секции [collectors]. ValueError — понятная ошибка конфига."""
    reg = registry()
    if section is None:
        section = {name: {} for name in DEFAULT_COLLECTORS}
    result = []
    for name, cfg in section.items():
        if name not in reg:
            raise ValueError(f"[collectors.{name}]: неизвестный коллектор (есть: {', '.join(sorted(reg))})")
        if not isinstance(cfg, dict):
            raise ValueError(f"[collectors.{name}] должна быть таблицей")
        if cfg.get("enabled", True) is False:
            continue
        result.append(reg[name](cfg, default_interval))
    return result


def safe_read(collector: Collector) -> dict[str, float]:
    """Чтение с защитой: исключение одного коллектора не роняет агент."""
    started = time.monotonic()
    try:
        values = clean_values(collector.read())
    except Exception as exc:  # noqa: BLE001 — коллектор не должен ронять цикл
        log.warning("коллектор %s: ошибка чтения: %s", collector.name, exc)
        return {}
    took = time.monotonic() - started
    if took > 2:
        log.info("коллектор %s читался %.1f с", collector.name, took)
    return values


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

# Окружение для внешних команд: без переменных агента (токены, прокси и т.п.)
if os.name == "nt":
    # Windows без SystemRoot не запускает даже системные утилиты
    SAFE_ENV = {k: os.environ[k] for k in ("PATH", "SystemRoot", "SystemDrive", "PATHEXT", "WINDIR") if k in os.environ}
else:
    SAFE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def run_argv(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Запуск списком аргументов, без shell, с таймаутом → (код, stdout, stderr)."""
    import subprocess

    p = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                       env=SAFE_ENV, check=False)
    return p.returncode, p.stdout.decode("utf-8", "replace")[:65536], p.stderr.decode("utf-8", "replace")[:65536]


def check_items(section: str, items, required: dict[str, type], optional: dict[str, type]) -> list[dict]:
    """Проверка списка таблиц вида [{name=..., ...}] из конфига коллектора."""
    if not isinstance(items, list):
        raise ValueError(f"[collectors.{section}]: ожидается список таблиц")
    seen = set()
    for i, item in enumerate(items):
        where = f"[collectors.{section}] элемент {i + 1}"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: ожидается таблица {{ name = ..., ... }}")
        unknown = set(item) - set(required) - set(optional)
        if unknown:
            raise ValueError(f"{where}: неизвестные ключи: {', '.join(sorted(unknown))}")
        for key, typ in required.items():
            if not isinstance(item.get(key), typ):
                raise ValueError(f"{where}: нужен ключ {key}")
        for key, typ in optional.items():
            if key in item and not isinstance(item[key], typ):
                raise ValueError(f"{where}: неверный тип {key}")
        name = item.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError(f"{where}: name — строчные латиница, цифры, _ и - (до 48 символов)")
        if name in seen:
            raise ValueError(f"{where}: повтор имени {name}")
        seen.add(name)
    return items


def norm_name(text: str) -> str:
    """Имя объекта для id датчика: строчные латиница/цифры/_ ."""
    text = re.sub(r"[^a-z0-9_]+", "_", text.strip().lower()).strip("_")
    return text or "unknown"
