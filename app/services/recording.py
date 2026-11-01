"""Запись вывода SSH-консоли в формате asciicast v2 (https://docs.asciinema.org/manual/asciicast/v2/).

Пишется ТОЛЬКО вывод терминала — то, что видел пользователь. Ввод не пишется никогда (пароли sudo и т.п.).
Запись копится в памяти сеанса, при закрытии сжимается gzip и кладётся в БД (`console_recordings`).
"""
import codecs
import gzip
import json
import time
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, defer

from app.models import ConsoleRecording
from app.services.settings_registry import get_setting
from app.utils import utcnow

TRUNCATED = "\r\n[запись обрезана: достигнут лимит console.record_max_mb]\r\n"


def wanted(db: Session, requested) -> bool:
    """off — не пишем никогда, always — всегда, optional — по галочке на странице (по умолчанию включена)."""
    mode = get_setting(db, "console.record")
    return mode == "always" or (mode == "optional" and requested is not False)


class Recorder:
    def __init__(self, cols: int, rows: int, max_bytes: int):
        self.cols, self.rows, self.max_bytes = cols, rows, max_bytes
        self.started = time.monotonic()
        self.timestamp = int(time.time())
        self.events: list[list] = []
        self.size = 0
        self.truncated = False
        # символ UTF-8 может лежать на границе двух чанков
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _t(self) -> float:
        return round(time.monotonic() - self.started, 6)

    def write(self, chunk: bytes) -> None:
        if self.truncated:
            return
        if self.size + len(chunk) > self.max_bytes:
            chunk = chunk[:max(0, self.max_bytes - self.size)]
            self.truncated = True
        self.size += len(chunk)
        text = self._decoder.decode(chunk)
        if text:
            self.events.append([self._t(), "o", text])
        if self.truncated:
            self.events.append([self._t(), "o", TRUNCATED])

    def resize(self, cols: int, rows: int) -> None:
        if not self.truncated:
            self.events.append([self._t(), "r", f"{cols}x{rows}"])

    def cast(self) -> str:
        header = {"version": 2, "width": self.cols, "height": self.rows, "timestamp": self.timestamp}
        lines = [json.dumps(header)] + [json.dumps(e, ensure_ascii=False) for e in self.events]
        return "\n".join(lines) + "\n"

    def save(self, db: Session, *, server_id: int, server_name: str, user_id: int, username: str, target: str,
             reason: str) -> int:
        now = utcnow()
        row = ConsoleRecording(
            server_id=server_id, server_name=server_name[:120], user_id=user_id, username=username[:64],
            target=target[:255], started_at=now - timedelta(seconds=self._t()), ended_at=now,
            bytes=self.size, data=gzip.compress(self.cast().encode()), reason=reason[:120])
        db.add(row)
        db.commit()
        return row.id


def cast_text(rec: ConsoleRecording) -> str:
    return gzip.decompress(rec.data).decode()


def cleanup(db: Session) -> None:
    """Старше console.record_days дней удаляются, закреплённые (pinned) — никогда."""
    border = utcnow() - timedelta(days=get_setting(db, "console.record_days"))
    db.execute(delete(ConsoleRecording).where(ConsoleRecording.pinned.is_(False), ConsoleRecording.ended_at < border))
    db.commit()


def listing(db: Session, server_id: int | None = None) -> list[ConsoleRecording]:
    # data не грузим: список не должен тянуть мегабайты
    q = select(ConsoleRecording).options(defer(ConsoleRecording.data)).order_by(ConsoleRecording.id.desc()).limit(500)
    if server_id:
        q = q.where(ConsoleRecording.server_id == server_id)
    return list(db.scalars(q))
