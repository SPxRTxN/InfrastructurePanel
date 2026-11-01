from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.servers import Server

DEVICE_TYPES = {
    "cloud": "Интернет / облако",
    "router": "Роутер",
    "switch": "Коммутатор",
    "ap": "Точка доступа",
    "server": "Сервер",
    "pc": "Компьютер",
    "iot": "IoT",
    "other": "Другое",
}


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(16), default="other")
    ip: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(String(1000), default="")
    # MAC в нижнем регистре через «:» (для Wake-on-LAN); пусто — не задан
    mac: Mapped[str] = mapped_column(String(17), default="", server_default="")
    # Координаты узла на графе; NULL — ещё не расставлен, сработает автоматическая раскладка
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id", ondelete="SET NULL"))
    # Имена портов по одному в строке; пусто — порты не заданы (в связях тогда свободный текст)
    ports: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Сегмент сети. Без FK-ограничения в БД (миграция только ADD COLUMN): SET NULL делает код
    # при удалении сегмента (см. routers/topology.segment_delete)
    segment_id: Mapped[int | None] = mapped_column(Integer, index=True)

    server: Mapped[Server | None] = relationship()


class Link(Base):
    __tablename__ = "links"

    id: Mapped[int] = mapped_column(primary_key=True)
    a_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    a_port: Mapped[str] = mapped_column(String(32), default="")
    b_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    b_port: Mapped[str] = mapped_column(String(32), default="")
    label: Mapped[str] = mapped_column(String(128), default="")
    # wired — кабель, wifi — беспроводное подключение (рисуется пунктиром)
    kind: Mapped[str] = mapped_column(String(8), default="wired", server_default="wired")

    a: Mapped[Device] = relationship(foreign_keys=[a_id])
    b: Mapped[Device] = relationship(foreign_keys=[b_id])


class Segment(Base):
    """Сегмент / кластер сети: на карте — рамка вокруг своих устройств."""
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    color: Mapped[str] = mapped_column(String(7), default="#3b82f6", server_default="#3b82f6")
    subnet: Mapped[str] = mapped_column(String(64), default="", server_default="")  # подсеть или VLAN, опционально
    description: Mapped[str] = mapped_column(String(500), default="", server_default="")
