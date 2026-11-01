"""Начальные данные домашней лаборатории (раздел 10 задания). Повторный запуск ничего не дублирует."""
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Device, Link, Server
from app.services.permissions import ensure_default_groups

SERVERS = [
    # имя, адрес, сбор, включён
    ("Сервер (homelab)", "127.0.0.1", "local", True),
    ("Raspberry Pi", "192.168.1.10", "ssh", False),  # ключ настроит владелец
]

# имя, тип, IP, x, y (начальная раскладка сверху вниз), сервер
DEVICES = [
    ("Интернет", "cloud", "", 0, 0, None),
    ("Роутер провайдера", "router", "192.168.1.1", 0, 130, None),
    ("MikroTik hEX S", "router", "192.168.1.1", 0, 260, None),
    ("Свитч LAB", "switch", "", -300, 400, None),
    ("Свитч HOME", "switch", "", 60, 400, None),
    ("Точка доступа (гости)", "ap", "192.168.1.3", 380, 400, None),
    ("Сервер (homelab)", "server", "192.168.1.39", -400, 540, "Сервер (homelab)"),
    ("Raspberry Pi", "server", "192.168.1.10", -200, 540, "Raspberry Pi"),
    ("ПК", "pc", "192.168.1.29", -60, 540, None),
    ("Точка доступа (дом)", "ap", "192.168.1.2", 80, 540, None),
    ("IoT-хаб", "iot", "192.168.1.50", 220, 540, None),
]

# устройство A, порт A, устройство B, порт B
LINKS = [
    ("Интернет", "", "Роутер провайдера", ""),
    ("Роутер провайдера", "", "MikroTik hEX S", "ether1"),
    ("MikroTik hEX S", "ether2", "Свитч LAB", ""),
    ("Свитч LAB", "", "Сервер (homelab)", ""),
    ("Свитч LAB", "", "Raspberry Pi", ""),
    ("MikroTik hEX S", "ether3", "Свитч HOME", ""),
    ("Свитч HOME", "", "ПК", ""),
    ("Свитч HOME", "", "Точка доступа (дом)", ""),
    ("Свитч HOME", "", "IoT-хаб", ""),
    ("MikroTik hEX S", "ether4", "Точка доступа (гости)", ""),
]


def seed(db: Session) -> dict[str, int]:
    ensure_default_groups(db)
    created = {"servers": 0, "devices": 0, "links": 0}

    servers: dict[str, Server] = {}
    for name, host, collector, enabled in SERVERS:
        server = db.scalar(select(Server).where(Server.name == name))
        if not server:
            server = Server(name=name, host=host, collector=collector, enabled=enabled, ssh_port=22)
            db.add(server)
            created["servers"] += 1
        servers[name] = server
    db.flush()

    devices: dict[str, Device] = {}
    for name, dtype, ip, x, y, server_name in DEVICES:
        device = db.scalar(select(Device).where(Device.name == name))
        if not device:
            device = Device(name=name, type=dtype, ip=ip, x=x, y=y,
                            server_id=servers[server_name].id if server_name else None)
            db.add(device)
            created["devices"] += 1
        devices[name] = device
    db.flush()

    for a_name, a_port, b_name, b_port in LINKS:
        a, b = devices[a_name], devices[b_name]
        exists = db.scalar(select(Link).where(or_(
            (Link.a_id == a.id) & (Link.b_id == b.id), (Link.a_id == b.id) & (Link.b_id == a.id))))
        if not exists:
            db.add(Link(a_id=a.id, a_port=a_port, b_id=b.id, b_port=b_port))
            created["links"] += 1
    db.commit()
    return created
