"""Создание устройства SSD1306/SSD1315 через luma.oled. Нет библиотеки или дисплея — None и предупреждение."""
from __future__ import annotations

import logging

from .base import DisplayConfig

log = logging.getLogger(__name__)


def create_device(cfg: DisplayConfig):
    try:
        from luma.core.interface.serial import i2c
        from luma.oled.device import ssd1306
    except ImportError:
        log.warning("дисплей: luma.oled не установлен — работаю без дисплея")
        return None
    try:
        serial = i2c(port=cfg.i2c_port, address=cfg.address)
        return ssd1306(serial, width=cfg.width, height=cfg.height, rotate=cfg.rotate)
    except Exception as exc:  # noqa: BLE001 — DeviceNotFoundError, PermissionError, OSError шины
        log.warning("дисплей не найден (I2C %d, адрес 0x%02X): %s — работаю без дисплея",
                    cfg.i2c_port, cfg.address, exc)
        return None
