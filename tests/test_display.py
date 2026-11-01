import asyncio
import logging
import os
import time
from datetime import datetime

import pytest

# Дисплейные зависимости — опция (requirements-display.txt); без них тесты дисплея пропускаются
pytest.importorskip("PIL")
from PIL import ImageChops, ImageFont  # noqa: E402

from homelab_agent.config import parse_config
from homelab_agent.display.base import (
    SHIFTS, Display, DisplayConfig, DisplayState, is_night, parse_display, render, screens,
)
from homelab_agent.display.screens import human_bytes, human_duration
from homelab_agent.display.ssd1306 import create_device
from homelab_agent.runner import Agent

FONTS = (ImageFont.load_default(10), ImageFont.load_default(13))  # детерминированно, без системных шрифтов


def state(**kw):
    base = dict(
        hostname="raspberry-pi", ip="192.168.1.10", iface="eth0",
        values={"thermal.cpu.temp": 51.2, "cpu.percent": 12.0, "cpu.load1": 0.52,
                "memory.used": 412_000_000, "memory.total": 948_000_000,
                "disks./.used": 12 * 2**30, "disks./.total": 29 * 2**30, "uptime.seconds": 90061},
        panels=[("dashboard.lan", True), ("192.168.1.10:8443", False)],
        summary={"online": 4, "total": 5, "alert": "Mini PC: диск / 91 %"},
        last_action={"name": "restart_nginx", "ok": True, "ts": time.time(), "source": "panel"},
        agent_started=time.time() - 3600, now=time.time(),
    )
    base.update(kw)
    return DisplayState(**base)


def lit(img) -> int:
    return sum(1 for p in img.getdata() if p)


def test_screens_registered():
    assert {"network", "health", "panels", "uptime"} <= set(screens())


@pytest.mark.parametrize("name", ["network", "health", "panels", "uptime"])
def test_each_screen_draws_within_bounds(name):
    img = render(name, state(), fonts=FONTS)
    assert img.size == (128, 64) and img.mode == "1"
    assert lit(img) > 30
    # Пустое состояние тоже не падает
    assert lit(render(name, DisplayState(), fonts=FONTS)) > 0


def test_network_shows_ip():
    a = render("network", state(ip="192.168.1.10"), fonts=FONTS)
    b = render("network", state(ip="192.168.1.11"), fonts=FONTS)
    assert ImageChops.difference(a, b).getbbox() is not None
    # Нижняя половина (IP) отличается, верхняя (hostname) — нет
    assert ImageChops.difference(a.crop((0, 0, 128, 20)), b.crop((0, 0, 128, 20))).getbbox() is None


def test_long_text_is_truncated():
    img = render("network", state(hostname="very-long-hostname-that-does-not-fit-at-all"), fonts=FONTS)
    bbox = img.getbbox()
    assert bbox[2] <= 128


def test_offset_shifts_content():
    a = render("network", state(), fonts=FONTS, offset=(0, 0))
    b = render("network", state(), fonts=FONTS, offset=(2, 1))
    assert b.getbbox()[0] == a.getbbox()[0] + 2


def test_formatters():
    assert human_bytes(948_000_000) == "904M"
    assert human_bytes(29 * 2**30) == "29.0G"
    assert human_duration(90061) == "1d 1h 1m"
    assert human_duration(None) == "?"


def test_parse_display_validation():
    assert parse_display({"enabled": True}).address == 0x3C
    for bad in ({"screens": ["nope"]}, {"rotate": 5}, {"driver": "sh1106"}, {"night_start": "25:00"},
                {"contrast": 300}, {"bogus": 1}, {"address": "0x3C"}):
        with pytest.raises(ValueError):
            parse_display(bad)


def test_is_night_wraps_midnight():
    cfg = DisplayConfig(night_start="23:00", night_end="07:00")
    assert is_night(cfg, datetime(2026, 1, 1, 23, 30))
    assert is_night(cfg, datetime(2026, 1, 1, 6, 59))
    assert not is_night(cfg, datetime(2026, 1, 1, 12, 0))
    day = DisplayConfig(night_start="01:00", night_end="05:00")
    assert is_night(day, datetime(2026, 1, 1, 3, 0)) and not is_night(day, datetime(2026, 1, 1, 6, 0))


class SpyDummy:
    """Обёртка над luma dummy: запоминает контраст."""

    def __init__(self):
        from luma.core.device import dummy

        self.dev = dummy(width=128, height=64, mode="1")
        self.width, self.height, self.mode = 128, 64, "1"
        self.contrasts = []

    def display(self, img):
        self.dev.display(img)

    def contrast(self, value):
        self.contrasts.append(value)
        self.dev.contrast(value)

    def hide(self):
        pass


def test_display_on_luma_dummy_switches_screens():
    pytest.importorskip("luma.core.device")
    dev = SpyDummy()
    cfg = DisplayConfig(screens=["network", "health"], switch_every=5, night_start="00:00", night_end="00:00")
    d = Display(dev, cfg, lambda: state())
    d.fonts = FONTS
    t0 = 1_000_000_000 - (1_000_000_000 % 10)  # начало цикла из двух экранов
    d.tick(t0)
    assert d.current == "network"
    img1 = dev.dev.image.copy()
    assert lit(img1) > 30
    offset = SHIFTS[int(t0 // 60) % len(SHIFTS)]
    expected = render("network", state(now=t0), fonts=FONTS, offset=offset)
    assert ImageChops.difference(img1, expected).getbbox() is None
    d.tick(t0 + 5)
    assert d.current == "health"
    assert ImageChops.difference(img1, dev.dev.image).getbbox() is not None
    assert dev.contrasts == [255]  # контраст ставится один раз, без лишних команд


def test_night_contrast_and_shift():
    pytest.importorskip("luma.core.device")
    dev = SpyDummy()
    cfg = DisplayConfig(screens=["network"], night_start="23:00", night_end="07:00", night_contrast=1)
    d = Display(dev, cfg, lambda: state())
    d.fonts = FONTS
    night = datetime(2026, 1, 1, 2, 0).timestamp()
    d.tick(night)
    assert dev.contrasts[-1] == 1
    bbox_a = dev.dev.image.getbbox()
    d.tick(night + 60)  # следующий сдвиг против выгорания
    bbox_b = dev.dev.image.getbbox()
    assert (bbox_b[0] - bbox_a[0], bbox_b[1] - bbox_a[1]) != (0, 0)
    assert all(0 <= dx <= 2 and 0 <= dy <= 2 for dx, dy in SHIFTS)


def test_missing_display_is_warning(caplog):
    if os.path.exists("/dev/i2c-99"):
        pytest.skip("неожиданно есть /dev/i2c-99")
    with caplog.at_level(logging.WARNING):
        assert create_device(DisplayConfig(i2c_port=99)) is None
    assert "дисплей" in caplog.text


def test_agent_runs_without_display_device(monkeypatch):
    """Дисплей включён, но не найден — агент работает дальше (без панелей тоже)."""
    monkeypatch.setattr("homelab_agent.display.ssd1306.create_device", lambda cfg: None)
    cfg = parse_config({"node": "pi", "display": {"enabled": True}})
    agent = Agent(cfg, collectors=[])

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(agent.run(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await task

    asyncio.run(main())
    st = agent.display_state()
    assert st.panels == [] and st.hostname


def test_agent_display_state_with_dummy(monkeypatch):
    pytest.importorskip("luma.core.device")
    dev = SpyDummy()
    monkeypatch.setattr("homelab_agent.display.ssd1306.create_device", lambda cfg: dev)
    cfg = parse_config({"node": "pi", "display": {"enabled": True, "screens": ["health"]}})
    agent = Agent(cfg, collectors=[])
    agent.publish({"thermal.cpu.temp": 48.0, "memory.used": 1, "memory.total": 2})

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(agent.run(stop))
        await asyncio.sleep(0.3)
        stop.set()
        await task

    asyncio.run(main())
    assert lit(dev.dev.image) > 30
