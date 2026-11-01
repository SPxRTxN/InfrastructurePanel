"""Экраны дисплея: каждый файл — один экран с `@screen("имя")`. Общие форматтеры — здесь."""


def human_bytes(n) -> str:
    if n is None:
        return "?"
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit in ("B", "K", "M") else f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def human_duration(seconds) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    d, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"
