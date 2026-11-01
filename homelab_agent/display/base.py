"""OLED-дисплей 128×64: состояние, реестр экранов, переключение, защита от выгорания.

Новый экран = один файл в `display/screens/`:

    @screen("name")
    def draw(ctx: Ctx) -> None: ...

Отрисовка — чистый Pillow (картинка режима "1"), устройство luma только
получает готовое изображение, поэтому экраны тестируются без железа.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import pkgutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

log = logging.getLogger(__name__)

DISPLAY_KEYS = {"enabled", "driver", "i2c_port", "address", "rotate", "screens", "switch_every", "contrast",
                "night_contrast", "night_start", "night_end", "shift_every", "font", "width", "height"}
DEFAULT_SCREENS = ["network", "health", "panels", "uptime"]
# Сдвиги содержимого против выгорания: 0–2 px по кругу
SHIFTS = [(0, 0), (1, 0), (2, 1), (1, 2), (0, 1)]
FONT_CANDIDATES = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]

_SCREENS: dict[str, Callable] = {}


def screen(name: str):
    """Декоратор: регистрирует функцию отрисовки экрана."""

    def deco(fn):
        _SCREENS[name] = fn
        return fn

    return deco


def screens() -> dict[str, Callable]:
    if not getattr(screens, "_loaded", False):
        pkg = importlib.import_module(f"{__package__}.screens")
        for mod in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(f"{pkg.__name__}.{mod.name}")
        screens._loaded = True
    return dict(_SCREENS)


@dataclass
class DisplayState:
    """Всё, что показывают экраны. Собирается агентом раз в секунду."""
    hostname: str = ""
    ip: str = ""
    iface: str = ""
    values: dict = field(default_factory=dict)
    panels: list[tuple[str, bool]] = field(default_factory=list)  # (имя, подключена)
    summary: dict | None = None
    last_action: dict | None = None
    agent_started: float = 0.0
    now: float = 0.0


@dataclass
class DisplayConfig:
    driver: str = "ssd1306"
    i2c_port: int = 1
    address: int = 0x3C
    rotate: int = 0
    screens: list[str] = field(default_factory=lambda: list(DEFAULT_SCREENS))
    switch_every: float = 5
    contrast: int = 255
    night_contrast: int = 1
    night_start: str = "23:00"
    night_end: str = "07:00"
    shift_every: float = 60
    font: str | None = None
    width: int = 128
    height: int = 64


def _hhmm(value: str, key: str) -> int:
    try:
        h, m = value.split(":")
        minutes = int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        raise ValueError(f'[display] {key}: время вида "23:00"') from None
    if not 0 <= minutes < 24 * 60:
        raise ValueError(f'[display] {key}: время вида "23:00"')
    return minutes


def parse_display(section: dict | None) -> DisplayConfig:
    try:
        return _parse_display(section)
    except TypeError:
        raise ValueError("[display]: неверный тип значения (числа без кавычек, время — строкой)") from None


def _parse_display(section: dict | None) -> DisplayConfig:
    raw = dict(section or {})
    unknown = set(raw) - DISPLAY_KEYS
    if unknown:
        raise ValueError(f"[display]: неизвестные ключи: {', '.join(sorted(unknown))}")
    raw.pop("enabled", None)
    cfg = DisplayConfig(**raw)
    if cfg.driver != "ssd1306":
        raise ValueError('[display] driver: поддерживается "ssd1306" (SSD1315 совместим)')
    if cfg.rotate not in (0, 1, 2, 3):
        raise ValueError("[display] rotate: 0, 1, 2 или 3 (×90°)")
    if not 0x03 <= cfg.address <= 0x77:
        raise ValueError("[display] address: адрес I2C, обычно 0x3C")
    known = screens()
    bad = [s for s in cfg.screens if s not in known]
    if not cfg.screens or bad:
        raise ValueError(f"[display] screens: неизвестные экраны {bad}; есть: {', '.join(sorted(known))}")
    if cfg.switch_every < 1 or cfg.shift_every < 1:
        raise ValueError("[display] switch_every и shift_every — секунды ≥ 1")
    for key in ("contrast", "night_contrast"):
        if not 0 <= getattr(cfg, key) <= 255:
            raise ValueError(f"[display] {key}: 0..255")
    _hhmm(cfg.night_start, "night_start")
    _hhmm(cfg.night_end, "night_end")
    return cfg


def is_night(cfg: DisplayConfig, when: datetime) -> bool:
    start, end = _hhmm(cfg.night_start, "night_start"), _hhmm(cfg.night_end, "night_end")
    cur = when.hour * 60 + when.minute
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)


def load_fonts(path: str | None):
    """(мелкий, крупный) шрифт. DejaVu — ради кириллицы в тексте тревоги от панели."""
    from PIL import ImageFont

    for candidate in ([path] if path else []) + FONT_CANDIDATES:
        if candidate and os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, 10), ImageFont.truetype(candidate, 13)
            except OSError:
                continue
    return ImageFont.load_default(10), ImageFont.load_default(13)


class Ctx:
    """Контекст отрисовки экрана: холст, шрифты, сдвиг, помощники."""

    def __init__(self, draw, state: DisplayState, width: int, height: int, fonts, offset=(0, 0)):
        self.draw, self.state, self.width, self.height = draw, state, width, height
        self.small, self.big = fonts
        self.dx, self.dy = offset

    def fit(self, text: str, font) -> str:
        """Обрезает строку по ширине экрана с «…»."""
        limit = self.width - 2 - self.dx
        if font.getlength(text) <= limit:
            return text
        while text and font.getlength(text + "…") > limit:
            text = text[:-1]
        return text + "…"

    def text(self, x: int, y: int, text: str, big: bool = False) -> None:
        font = self.big if big else self.small
        self.draw.text((x + self.dx, y + self.dy), self.fit(text, font), font=font, fill=1)

    def value(self, key: str):
        return self.state.values.get(key)


def render(name: str, state: DisplayState, width: int = 128, height: int = 64, fonts=None, offset=(0, 0)):
    """Рисует экран в картинку PIL режима "1" (1 бит на пиксель, как у OLED)."""
    from PIL import Image, ImageDraw

    img = Image.new("1", (width, height), 0)
    ctx = Ctx(ImageDraw.Draw(img), state, width, height, fonts or load_fonts(None), offset)
    screens()[name](ctx)
    return img


class Display:
    """Переключение экранов на устройстве luma (или любом объекте с display()/contrast())."""

    def __init__(self, device, cfg: DisplayConfig, state_provider: Callable[[], DisplayState]):
        self.device = device
        self.cfg = cfg
        self.state_provider = state_provider
        self.fonts = load_fonts(cfg.font)
        self._contrast: int | None = None
        self.current: str | None = None

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        idx = int(now // self.cfg.switch_every) % len(self.cfg.screens)
        self.current = self.cfg.screens[idx]
        offset = SHIFTS[int(now // self.cfg.shift_every) % len(SHIFTS)]
        contrast = self.cfg.night_contrast if is_night(self.cfg, datetime.fromtimestamp(now)) else self.cfg.contrast
        if contrast != self._contrast:
            self.device.contrast(contrast)
            self._contrast = contrast
        state = self.state_provider()
        state.now = now
        w, h = getattr(self.device, "width", self.cfg.width), getattr(self.device, "height", self.cfg.height)
        img = render(self.current, state, w, h, self.fonts, offset)
        self.device.display(img.convert(getattr(self.device, "mode", "1")))

    async def run(self, stop: asyncio.Event) -> None:
        log.info("дисплей: экраны %s, смена каждые %g с", ", ".join(self.cfg.screens), self.cfg.switch_every)
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception as exc:  # noqa: BLE001 — сбой дисплея не должен ронять агент
                log.warning("дисплей: ошибка отрисовки: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        try:
            self.device.hide()  # гасим экран при остановке службы
        except Exception:  # noqa: BLE001
            pass


def check_display_config(section: dict) -> list[str]:
    """Для --check-config: валидность секции, наличие luma и шины I2C."""
    try:
        cfg = parse_display(section)
    except (ValueError, TypeError) as exc:
        return [str(exc)]
    problems = []
    try:
        import luma.oled.device  # noqa: F401
    except ImportError:
        problems.append("[display]: не установлен luma.oled (pip install -r requirements-display.txt)")
    bus = f"/dev/i2c-{cfg.i2c_port}"
    if not os.path.exists(bus):
        problems.append(f"[display]: нет {bus} — включите I2C (raspi-config → Interface Options → I2C)")
    return problems
