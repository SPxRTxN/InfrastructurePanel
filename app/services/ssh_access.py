"""Куда и как подключаться по SSH: сервер, сохранённый вход (ОС / BMC) или команда вручную, ключ хоста accept-new.

Вручную — как в терминале: `ssh пользователь@адрес` или `ssh -p порт пользователь@адрес`; порт не указан — 22,
как у ssh. Адрес — только свой у сервера или у его сохранённых входов: консоль сервера не превращается в
переход на любую машину сети (для другой машины добавьте сервер).

Ключ хоста: при первом подключении к адресу+порту панель получает ключ с сервера и запоминает его в ssh_host_keys —
как `StrictHostKeyChecking=accept-new` у OpenSSH, без ssh-keyscan. Дальше подключение только с этим ключом:
сменился — отказ (возможна подмена), сброс — кнопкой на странице сервера, с записью в журнал.
"""
import asyncio
import errno
import getpass
import logging
import os
import re
import shlex
import socket
from dataclasses import dataclass

from app.models import Server, ServerCredential, SshHostKey
from app.services import agent_tunnel
from app.services.security import decrypt

TIMEOUT = 15
DEFAULT_PORT = 22  # как у ssh, если порт не указан
_USER = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._@-]{0,63}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$|^\[?[0-9A-Fa-f:.]{2,45}\]?$")
_URL = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>\[[^\]]+\]|[^:/@]+)(?::(?P<port>\d+))?/?$")


class BadTarget(Exception):
    """Команда подключения не разобрана или адрес не принадлежит серверу — текст для пользователя в args[0]."""


@dataclass
class Target:
    host: str
    port: int
    username: str
    password: str
    key_path: str
    host_key: str  # запомненный ключ хоста ("" — первое подключение)
    label: str
    node_id: int = 0  # не 0 - «через агента»: подключение к sshd узла по TCP-туннелю агента, host/port не используются

    @property
    def address(self) -> str:
        return f"{self.username}@{self.host}:{self.port}"


def ssh_command(user: str, host: str, port: int) -> str:
    """Та же команда, что набрали бы в терминале."""
    return f"ssh {user}@{host}" if port == DEFAULT_PORT else f"ssh -p {port} {user}@{host}"


def parse_command(text: str, default_user: str = "") -> tuple[str, str, int]:
    """`[ssh] [-p ПОРТ] [пользователь@]адрес` или `ssh://пользователь@адрес:порт` → (пользователь, адрес, порт)."""
    try:
        args = shlex.split(text.strip())
    except ValueError:
        raise BadTarget("команда не разобрана: проверьте кавычки") from None
    if args and args[0] == "ssh":
        args = args[1:]
    port, dest = None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-p":
            if i + 1 >= len(args):
                raise BadTarget("после -p нужен номер порта")
            port, i = args[i + 1], i + 2
            continue
        if a.startswith("-p") and len(a) > 2:
            port, i = a[2:], i + 1
            continue
        if a.startswith("-"):
            raise BadTarget(f"параметр {a} не поддерживается: только -p ПОРТ и пользователь@адрес")
        if dest is not None:
            raise BadTarget("укажите один адрес: ssh -p ПОРТ пользователь@адрес")
        dest, i = a, i + 1
    if not dest:
        raise BadTarget("укажите адрес: ssh пользователь@адрес")
    if dest.startswith("ssh://"):
        m = _URL.match(dest[6:])
        if not m:
            raise BadTarget("адрес вида ssh://пользователь@адрес:порт")
        dest, port = (f"{m['user']}@{m['host']}" if m["user"] else m["host"]), port or m["port"]
    user, _, host = dest.rpartition("@")
    user = user or default_user
    try:
        port = int(port) if port is not None else DEFAULT_PORT
    except ValueError:
        raise BadTarget("порт — число от 1 до 65535") from None
    if not 1 <= port <= 65535:
        raise BadTarget("порт — число от 1 до 65535")
    if not user or not _USER.match(user):
        raise BadTarget("укажите пользователя: ssh пользователь@адрес")
    if not _HOST.match(host):
        raise BadTarget("недопустимый адрес")
    return user, host.strip("[]"), port


def resolve(server: Server, cred: ServerCredential | None, command: str = "", password: str = "",
            via_node: int = 0) -> Target:
    """Сохранённый вход — его адрес, логин и пароль; без него — команда вручную. Ключ сервера — только для ОС.

    via_node (id узла) - через агента: адрес и порт из входа/команды не используются, всегда sshd узла по [terminal]
    агента; ключ хоста хранится отдельно от прямого (host = agent:<id>, port 22).
    """
    key_path = decrypt(server.ssh_key_enc) if server.ssh_key_enc else ""
    if via_node:
        if cred is not None and cred.kind != "os":
            raise BadTarget("BMC через агента недоступен: у него свой адрес, подключайтесь напрямую")
        if cred is not None:
            user, label, password = cred.username, cred.label or "OS", decrypt(cred.password_enc) if cred.password_enc else ""
        elif command.strip():
            user = parse_command(command, server.ssh_user)[0]  # адрес и порт игнорируются
            label = "вручную"
        else:
            user, label = server.ssh_user, "сервер"
        host = f"agent:{via_node}"
        known = server.known_key(host, DEFAULT_PORT)
        return Target(host, DEFAULT_PORT, user, password, key_path, known.key_line if known else "",
                      f"{label} через агента", via_node)
    if cred is None and not command.strip():
        # Мониторинг и вход без команды — поля сервера; пустой пользователь — как у ssh, локальное имя
        user, host, port, label = server.ssh_user, server.host, server.ssh_port, "сервер"
    elif cred is None:
        user, host, port = parse_command(command, server.ssh_user)
        if host not in server.allowed_hosts():
            raise BadTarget(f"адрес {host} не принадлежит серверу «{server.name}»: подключайтесь к {server.host} "
                            "или добавьте вход с этим адресом (BMC) либо отдельный сервер")
        label = "вручную"
    else:
        user, host, port = cred.username, cred.host or server.host, cred.port
        label, password = cred.label or cred.kind.upper(), decrypt(cred.password_enc) if cred.password_enc else ""
        if cred.kind != "os":
            key_path = ""  # ключ сервера — для его ОС, не для BMC
    known = server.known_key(host, port)
    return Target(host, port, user, password, key_path, known.key_line if known else "", label)


def remember_key(server: Server, host: str, port: int, line: str) -> None:
    """Сохранить ключ, полученный при первом подключении к адресу+порту."""
    if server.known_key(host, port) is None:
        server.host_keys.append(SshHostKey(host=host, port=port, key_line=line))


def fingerprint(line: str) -> str:
    if not line:
        return ""
    import asyncssh

    try:
        return asyncssh.import_public_key(line).get_fingerprint("sha256")
    except (ValueError, asyncssh.KeyImportError):
        return "?"


def _ensure_local_username() -> None:
    """asyncssh перед подключением узнаёт имя локального пользователя (getpass.getuser). В Docker панель работает
    от числового DASHBOARD_UID без записи в /etc/passwd — без этого любое подключение падает с ValueError."""
    if any(os.environ.get(v) for v in ("LOGNAME", "USER", "LNAME", "USERNAME")):
        return
    try:
        getpass.getuser()
    except (KeyError, OSError):
        os.environ["LOGNAME"] = "homelab-dashboard"


class KeyFileUnavailable(Exception):
    """Путь к ключу сервера задан, но файла нет или его не прочитать (в Docker — путь хоста вместо /home/app/.ssh)."""


class NoHostKey(Exception):
    """Сервер не прислал ключ хоста — проверить его нельзя, подключаться не будем."""


def _usable_key(target: Target) -> str:
    """Ключ сервера, если файл читается. Нет файла: есть пароль — входим по паролю, нет — понятная ошибка."""
    if not target.key_path:
        return ""
    if os.path.isfile(target.key_path) and os.access(target.key_path, os.R_OK):
        return target.key_path
    if target.password:
        return ""
    raise KeyFileUnavailable()


async def connect(target: Target):
    """Подключение с проверкой ключа хоста. Возвращает (conn, новый ключ строкой или None — если уже был)."""
    import asyncssh

    _ensure_local_username()
    key_path = _usable_key(target)
    # Ничего не берём из ~/.ssh контейнера (ключи по умолчанию, config, known_hosts, X.509 из ~/.ssh/crt, агент):
    # всё задано явно, а каталог ssh/ с хоста бывает недоступен пользователю контейнера — тогда asyncssh падал
    # с PermissionError на проверке ~/.ssh/crt (Path.is_dir не глотает EACCES)
    common = {"agent_path": None, "config": None, "x509_trusted_certs": None, "x509_trusted_cert_paths": None}
    new_line = None
    ref = _agent_ref(target) if target.node_id else None
    if target.host_key:
        key = asyncssh.import_public_key(target.host_key)
    else:
        probe = asyncssh.SSHClientConnectionOptions(client_keys=None, known_hosts=None, **common)
        if ref:  # ключ хоста берём по отдельному туннелю, потом закрываем его
            sock = await agent_tunnel.open(ref)
            try:
                key = await asyncio.wait_for(asyncssh.get_server_host_key(sock=sock, options=probe), TIMEOUT)
            finally:
                sock.close()
        else:
            key = await asyncio.wait_for(asyncssh.get_server_host_key(target.host, target.port, options=probe), TIMEOUT)
        if key is None:
            raise NoHostKey()
        new_line = key.export_public_key().decode().strip()
    kwargs = dict(username=target.username or None, client_keys=[key_path] if key_path else None,
                  password=target.password or None, known_hosts=([key], [], []), connect_timeout=TIMEOUT, **common)
    if ref is None:
        return await asyncssh.connect(target.host, port=target.port, **kwargs), new_line
    # Агент мог ещё не освободить сеанс пробы (при max_sessions = 1) - несколько коротких повторов
    for attempt in range(5):
        try:
            sock = await agent_tunnel.open(ref)
            break
        except agent_tunnel.TunnelBusy:
            if not new_line or attempt == 4:
                raise
            await asyncio.sleep(0.4)
    try:
        return await asyncssh.connect(sock=sock, **kwargs), new_line
    except BaseException:
        sock.close()
        raise


def _agent_ref(target: Target):
    """Снимок узла для туннеля (сессия БД закрывается до подключения)."""
    from app.db import SessionLocal
    from app.models import Node

    with SessionLocal() as db:
        node = db.get(Node, target.node_id)
        if node is None:
            raise agent_tunnel.TunnelError("узел агента не найден")
        return agent_tunnel.ref(node)


def error_text(exc: BaseException) -> str:
    """Понятная причина без подробностей исключения (в них бывают пути и адреса)."""
    name = exc.__class__.__name__
    if isinstance(exc, BadTarget):
        return str(exc)
    if isinstance(exc, agent_tunnel.TunnelError):
        return str(exc)
    if isinstance(exc, KeyFileUnavailable):
        return ("файл SSH-ключа сервера не найден или не читается внутри панели. В Docker укажите путь вида "
                "/home/app/.ssh/id_ed25519 (каталог ssh/ рядом с compose.yaml) или войдите по паролю")
    if isinstance(exc, NoHostKey):
        return "сервер не прислал ключ хоста — подключение без проверки ключа запрещено"
    if name == "HostKeyNotVerifiable":
        return ("ключ сервера изменился с первого подключения — возможна подмена. Если ОС или BMC переустанавливали, "
                "сбросьте ключ на странице сервера (блок «Доступ»)")
    if name in ("PermissionDenied", "KeyImportError"):
        return "вход не удался: проверьте пользователя, ключ или пароль"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "сервер не ответил вовремя (адрес недоступен из панели или порт закрыт брандмауэром)"
    if isinstance(exc, ConnectionRefusedError):
        return "порт SSH закрыт: на сервере не запущен sshd или указан не тот порт"
    if isinstance(exc, socket.gaierror):
        return "адрес не найден (ошибка в имени сервера или DNS)"
    if isinstance(exc, OSError) and exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return "нет маршрута до сервера: панель (контейнер) не видит эту сеть"
    # Остальное — подробность (с путём файла, если он есть) в журнал службы: docker compose logs
    logging.getLogger("uvicorn.error").warning("SSH: %s: %s", name, str(exc)[:300])
    if isinstance(exc, OSError):
        return f"нет подключения к серверу ({name}) — подробности в журнале службы панели"
    return f"ошибка SSH ({name}) — подробности в журнале службы панели"
