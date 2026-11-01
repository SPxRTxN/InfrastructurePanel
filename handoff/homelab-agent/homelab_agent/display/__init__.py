"""OLED-дисплей узла (SSD1306/SSD1315 128×64 по I2C)."""
from .base import Display, DisplayState, parse_display, render, screen, screens

__all__ = ["Display", "DisplayState", "parse_display", "render", "screen", "screens"]
