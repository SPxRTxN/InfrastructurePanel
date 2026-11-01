"""Коллекторы датчиков: каждый файл пакета — один источник (`@register`)."""
from .base import Collector, Sensor, build_collectors, register, registry, safe_read

__all__ = ["Collector", "Sensor", "build_collectors", "register", "registry", "safe_read"]
