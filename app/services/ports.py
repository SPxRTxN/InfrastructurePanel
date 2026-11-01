"""Порты устройств: шаблоны, разбор текста и занятость."""

MAX_PORTS = 64


def _seq(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i}" for i in range(1, n + 1)]


# ключ → название, подходящие типы устройств, порты
TEMPLATES: dict[str, dict] = {
    "switch5": {"label": "Свитч, 5 портов", "types": ["switch"], "ports": _seq("port", 5)},
    "switch8": {"label": "Свитч, 8 портов", "types": ["switch"], "ports": _seq("port", 8)},
    "switch16": {"label": "Свитч, 16 портов", "types": ["switch"], "ports": _seq("port", 16)},
    "switch24": {"label": "Свитч, 24 порта", "types": ["switch"], "ports": _seq("port", 24)},
    "mikrotik_hex_s": {"label": "MikroTik hEX S (ether1–5 + sfp1)", "types": ["router"],
                       "ports": _seq("ether", 5) + ["sfp1"]},
    "isp_router": {"label": "Роутер провайдера (LAN1–4 + WAN)", "types": ["router"],
                   "ports": _seq("LAN", 4) + ["WAN"]},
    "ap": {"label": "Точка доступа (LAN + Wi-Fi)", "types": ["ap"], "ports": ["LAN", "Wi-Fi"]},
    "pc": {"label": "Компьютер / сервер (eth0)", "types": ["pc", "server"], "ports": ["eth0"]},
}


def parse_ports(raw: str) -> list[str]:
    """Имена портов из текста (по строкам или через запятую): без пустых и повторов."""
    out: list[str] = []
    for part in raw.replace(",", "\n").splitlines():
        name = part.strip()[:32]
        if name and name not in out:
            out.append(name)
    return out[:MAX_PORTS]


def used_ports(links, device_id: int) -> dict[str, int]:
    """Занятые проводными связями порты устройства: имя порта → id связи (Wi-Fi порт не занимает)."""
    used: dict[str, int] = {}
    for lk in links:
        if lk.kind != "wired":
            continue
        if lk.a_id == device_id and lk.a_port:
            used[lk.a_port] = lk.id
        if lk.b_id == device_id and lk.b_port:
            used[lk.b_port] = lk.id
    return used
