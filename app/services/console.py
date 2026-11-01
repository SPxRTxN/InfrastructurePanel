"""SSH-консоль в браузере: одноразовые билеты и мост WebSocket ↔ SSH с PTY.

Зачем билет: у WebSocket нет CSRF-заголовка, а cookie сессии браузер отправит и со страницы чужого сайта
(cross-site WebSocket hijacking). Билет выдаёт POST с CSRF-токеном; он одноразовый, живёт 30 с и привязан
к пользователю, его сессии и серверу. Билеты и счётчики — в памяти процесса (панель работает одним процессом).

Ввод и вывод терминала не сохраняются нигде: в журнал безопасности — только кто, куда, когда и сколько длилось.
Вход — сохранённый (server_credentials) или вручную: введённый пароль приходит в первом сообщении и не сохраняется.
Куда подключаться и проверка ключа хоста — app/services/ssh_access.py.
"""
import json
import secrets
import time
from dataclasses import dataclass

from app.services import ssh_access
from app.services.security import hash_token

TICKET_TTL = 30  # с
MAX_PER_USER = 10  # открытых консолей (вкладок) на пользователя — всего, по всем серверам
MAX_MESSAGE = 64 * 1024  # байт в одном сообщении из браузера (вставка большого текста)
TERM = "xterm-256color"


@dataclass
class Ticket:
    user_id: int
    session_hash: str
    server_id: int
    expires: float


_tickets: dict[str, Ticket] = {}  # sha256 билета → билет (сам билет не храним)
_open: dict[int, int] = {}  # user_id → открытых консолей


def _purge() -> None:
    now = time.monotonic()
    for key in [k for k, t in _tickets.items() if t.expires < now]:
        del _tickets[key]


def issue(user_id: int, session_hash: str, server_id: int) -> str:
    _purge()
    token = secrets.token_urlsafe(32)
    _tickets[hash_token(token)] = Ticket(user_id, session_hash, server_id, time.monotonic() + TICKET_TTL)
    return token


def redeem(token: str, server_id: int) -> Ticket | None:
    """Билет сгорает при первой попытке, даже неудачной (другой сервер)."""
    _purge()
    ticket = _tickets.pop(hash_token(token or ""), None)
    if ticket is None or ticket.server_id != server_id:
        return None
    return ticket


def acquire(user_id: int) -> bool:
    if _open.get(user_id, 0) >= MAX_PER_USER:
        return False
    _open[user_id] = _open.get(user_id, 0) + 1
    return True


def release(user_id: int) -> None:
    left = _open.get(user_id, 0) - 1
    if left > 0:
        _open[user_id] = left
    else:
        _open.pop(user_id, None)


def clamp_size(cols, rows) -> tuple[int, int]:
    def num(v, lo, hi, default):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default
    return num(cols, 20, 500, 80), num(rows, 5, 200, 24)


def parse(raw: str) -> dict | None:
    """Сообщение браузера: {"t": "i", "d": "..."} — ввод, {"t": "r", "c": cols, "r": rows} — размер."""
    if len(raw) > MAX_MESSAGE:
        return None
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) else None


error_text = ssh_access.error_text


async def open_shell(target: ssh_access.Target, cols: int, rows: int):
    """Подключение и интерактивный shell с PTY. Возвращает (conn, process, новый ключ хоста или None)."""
    import asyncssh

    conn, new_key = await ssh_access.connect(target)
    try:
        process = await conn.create_process(term_type=TERM, term_size=(cols, rows), encoding=None,
                                            stderr=asyncssh.STDOUT)
    except BaseException:
        conn.close()
        raise
    return conn, process, new_key
