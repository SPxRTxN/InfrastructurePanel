"""TCP-туннель через агента к его локальному sshd (второй путь SSH-консоли и передачи файлов).

Панель просит агента `tunnel_open` (без адреса: агент подключается только к своему `[terminal] target`),
получает `tunnel_opened` и отдаёт вызывающему один конец socketpair; второй конец качается в/из WebSocket агента
(`tunnel_data`, base64). SSH поверх туннеля делает asyncssh панели - агент видит только зашифрованные байты.
"""
import asyncio
import base64
import binascii
import itertools
import logging
import socket
from dataclasses import dataclass, field

from app.services import agents

log = logging.getLogger("uvicorn.error")

OPEN_TIMEOUT = 10
CHUNK = 32 * 1024
MAX_BUFFERED = 4 * 1024 * 1024  # агент прислал больше, чем читает asyncssh, - обрываем туннель
_ids = itertools.count(1)


class TunnelError(Exception):
    """Причина отказа для пользователя в args[0]."""


class TunnelBusy(TunnelError):
    pass


@dataclass(frozen=True)
class NodeRef:
    """Всё нужное от узла, чтобы не держать сессию БД, пока идёт подключение."""
    id: int
    name: str
    features: tuple
    tunnel_max: int | None


@dataclass
class Link:
    node_id: int
    tid: str
    local: socket.socket  # конец, который остался у панели (его отдали вызывающему - закрывается им)
    agent_side: socket.socket
    writer: asyncio.StreamWriter | None = None
    early: list = field(default_factory=list)  # данные, пришедшие раньше, чем готов writer
    task: asyncio.Task | None = None
    closed: bool = False


_pending: dict[tuple[int, str], asyncio.Future] = {}
_links: dict[tuple[int, str], Link] = {}


def ref(node) -> NodeRef:
    info = node.info
    return NodeRef(node.id, node.name, tuple(info.get("features", [])), info.get("tunnel_max"))


def available(node) -> bool:
    """Есть ли смысл предлагать «через агента»: узел на связи и объявил tunnel."""
    return node.id in agents.hub.sockets and "tunnel" in node.info.get("features", [])


def precheck(r: NodeRef) -> None:
    if r.id not in agents.hub.sockets:
        raise TunnelError("агент не в сети")
    if "tunnel" not in r.features:
        raise TunnelError("на узле терминал выключен ([terminal] enabled = false в конфиге агента; "
                          "нужна и версия агента с туннелем)")


async def open(node) -> socket.socket:  # noqa: A001 - имя из ТЗ: agent_tunnel.open(node)
    """Открыть туннель; вернуть сокет, по которому идут байты sshd узла (для asyncssh.connect(sock=...))."""
    r = node if isinstance(node, NodeRef) else ref(node)
    precheck(r)
    tid = f"t-{next(_ids)}"
    key = (r.id, tid)
    fut = asyncio.get_running_loop().create_future()
    _pending[key] = fut
    try:
        if not await agents.hub.send(r.id, {"type": "tunnel_open", "id": tid}):
            raise TunnelError("агент не в сети")
        try:
            res = await asyncio.wait_for(fut, OPEN_TIMEOUT)
        except asyncio.TimeoutError:
            await agents.hub.send(r.id, {"type": "tunnel_close", "id": tid, "reason": "open timeout"})
            _drop(key)
            raise TunnelError(f"агент не ответил за {OPEN_TIMEOUT} с") from None
    finally:
        _pending.pop(key, None)
    if isinstance(res, str):  # причина отказа
        if res == "busy":
            raise TunnelBusy(f"агент занят: открыто максимум сеансов ({r.tunnel_max or '?'})")
        if res == "disabled":
            raise TunnelError("на узле терминал выключен ([terminal] enabled = false)")
        if res == "connect failed":
            raise TunnelError("агент не смог подключиться к sshd узла: проверьте [terminal] target и что sshd запущен")
        raise TunnelError("агент отказал в туннеле")
    link = res
    reader, link.writer = await asyncio.open_connection(sock=link.agent_side)
    for data in link.early:
        link.writer.write(data)
    link.early.clear()
    link.task = asyncio.create_task(_pump(link, reader))
    return link.local


def _drop(key) -> None:
    link = _links.pop(key, None)
    if link:
        link.closed = True
        (link.writer or link.agent_side).close()
        if link.task:
            link.task.cancel()


async def _pump(link: Link, reader: asyncio.StreamReader) -> None:
    """Из сокета вызывающего (asyncssh) -> агенту. Конец потока = закрытие туннеля."""
    key = (link.node_id, link.tid)
    try:
        while data := await reader.read(CHUNK):
            if not await agents.hub.send(link.node_id, {"type": "tunnel_data", "id": link.tid,
                                                        "d": base64.b64encode(data).decode()}):
                break
    except (OSError, asyncio.CancelledError):
        pass
    if not link.closed:  # закрыла сторона панели: сообщаем агенту
        await agents.hub.send(link.node_id, {"type": "tunnel_close", "id": link.tid, "reason": "closed by panel"})
    _links.pop(key, None)
    link.closed = True
    link.writer.close()


def _pair() -> tuple[socket.socket, socket.socket]:
    """Пара соединённых сокетов. Не socket.socketpair(): у AF_UNIX нет peername, а asyncssh без него не стартует.
    Поэтому TCP на 127.0.0.1: слушающий порт живёт долю миллисекунды, принимается ровно одно соединение,
    и оно проверяется по адресу клиента."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        b = socket.create_connection(srv.getsockname())  # очередь listen принимает соединение до accept
        a, peer = srv.accept()
    if peer != b.getsockname():  # кто-то успел раньше - не используем
        a.close()
        b.close()
        raise TunnelError("не удалось создать локальный канал")
    a.setblocking(False)
    b.setblocking(False)
    return a, b


def on_message(node_id: int, data: dict) -> None:
    """tunnel_opened / tunnel_data / tunnel_close от агента (вызывается из цикла чтения WebSocket, без БД)."""
    tid = data.get("id")
    if not isinstance(tid, str):
        return
    key = (node_id, tid)
    kind = data.get("type")
    if kind == "tunnel_opened":
        fut = _pending.get(key)
        if fut is None or fut.done():
            return
        if data.get("ok") is True:
            try:
                a, b = _pair()
            except (OSError, TunnelError):  # не должно ломать цикл чтения WebSocket агента
                fut.set_result("refused")
                return
            _links[key] = Link(node_id, tid, b, a)
            fut.set_result(_links[key])
        else:
            detail = data.get("detail")
            fut.set_result(detail if isinstance(detail, str) and detail else "refused")
    elif kind == "tunnel_data":
        link = _links.get(key)
        if link is None or link.closed:
            return
        try:
            chunk = base64.b64decode(data.get("d") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            return
        if link.writer is None:
            link.early.append(chunk)
        elif link.writer.transport.get_write_buffer_size() > MAX_BUFFERED:
            link.writer.close()  # pump увидит конец потока и сам пошлёт агенту tunnel_close
        else:
            link.writer.write(chunk)
    elif kind == "tunnel_close":
        link = _links.pop(key, None)
        if link and not link.closed:
            link.closed = True
            if link.writer:
                link.writer.close()  # вызывающий увидит EOF
            else:
                link.agent_side.close()


def drop_node(node_id: int) -> None:
    """Агент отключился: все туннели узла закрываются, ждущие open получают отказ."""
    for key, fut in list(_pending.items()):
        if key[0] == node_id and not fut.done():
            fut.set_exception(TunnelError("агент не в сети"))
    for key in [k for k in _links if k[0] == node_id]:
        link = _links.pop(key)
        link.closed = True
        if link.writer:
            link.writer.close()
        else:
            link.agent_side.close()
