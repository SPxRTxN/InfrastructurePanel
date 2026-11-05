"""SSH-доступ: права, билет, Origin, мост к SSH, сохранённые входы, ключи хостов (accept-new), передача файлов.

Сетевые тесты — только с SSH-сервером asyncssh, поднятым в тесте на 127.0.0.1 (SFTP — в chroot временного каталога).
"""
import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.models import Group, GroupPermission, SecurityEvent, Server, ServerCredential, SshHostKey
from app.services import console, ssh_access
from app.services.security import decrypt, encrypt
from app.services.settings_registry import set_setting

ORIGIN = {"origin": "http://testserver"}


# ---------- Фейковый shell для моста WebSocket ----------

class FakeStdout:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    async def read(self, n):
        return await self.queue.get()


class FakeStdin:
    def __init__(self, out: FakeStdout):
        self.out = out

    def write(self, data: bytes):
        self.out.queue.put_nowait(b"echo:" + data)  # «сервер» отвечает эхом
        if data == b"exit\r":
            self.out.queue.put_nowait(b"")  # конец сеанса


class FakeProcess:
    def __init__(self):
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self.stdout)
        self.sizes = []

    def change_terminal_size(self, cols, rows):
        self.sizes.append((cols, rows))


class FakeConn:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def server(db):
    s = Server(name="pi", host="192.0.2.10", collector="ssh", ssh_user="user", ssh_key_enc=encrypt("/secret/key"))
    db.add(s)
    db.commit()
    return s


@pytest.fixture
def fake_ssh(monkeypatch):
    calls = {"new_key": None}

    async def open_shell(target, cols, rows):
        calls.update(target=ssh_access.Target(**vars(target)), size=(cols, rows))
        calls["process"], calls["conn"] = FakeProcess(), FakeConn()
        return calls["conn"], calls["process"], calls["new_key"]

    monkeypatch.setattr(console, "open_shell", open_shell)
    console._tickets.clear()
    console._open.clear()
    return calls


def _ticket(client: TestClient, server_id: int) -> str:
    r = client.post(f"/servers/{server_id}/console/ticket")
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


def _session(client, server_id, first: dict, script=("exit\r",)):
    with client.websocket_connect(f"/servers/{server_id}/console/ws", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"ticket": _ticket(client, server_id), **first}))
        msg = json.loads(ws.receive_text())
        if msg["t"] != "o":
            return msg
        for line in script:
            ws.send_text(json.dumps({"t": "i", "d": line}))
            ws.receive_bytes()
        return msg


# ---------- Права и защита подключения ----------

def test_permission_admin_only(admin, user, viewer, server):
    assert admin.get(f"/servers/{server.id}/console").status_code == 200
    assert "Консоль" in admin.get(f"/servers/{server.id}").text
    assert 'id="access"' in admin.get(f"/servers/{server.id}/settings").text  # входы и ключи — в настройках узла
    for c in (user, viewer):
        assert c.get(f"/servers/{server.id}/console").status_code == 403
        assert c.post(f"/servers/{server.id}/console/ticket").status_code == 403
        assert c.post(f"/servers/{server.id}/credentials", data={"username": "x"}).status_code == 403
        assert c.put(f"/servers/{server.id}/files?name=a", content=b"x").status_code == 403
        page = c.get(f"/servers/{server.id}").text
        assert f"/servers/{server.id}/console" not in page and 'id="access"' not in page


def test_page_does_not_show_key_path(admin, server):
    assert "/secret/key" not in admin.get(f"/servers/{server.id}/console").text


def test_ticket_requires_csrf(admin, server):
    del admin.headers["X-CSRF-Token"]
    assert admin.post(f"/servers/{server.id}/console/ticket").status_code == 403
    assert admin.put(f"/servers/{server.id}/files?name=a", content=b"x").status_code == 403


def test_foreign_origin_rejected(admin, server, fake_ssh):
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(f"/servers/{server.id}/console/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_text()
    assert "process" not in fake_ssh


def test_session_relay_and_audit(admin, server, fake_ssh, db):
    ticket = _ticket(admin, server.id)
    with admin.websocket_connect(f"/servers/{server.id}/console/ws", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"ticket": ticket, "cmd": "ssh user@192.0.2.10", "password": "hunter2",
                                 "c": 9999, "r": 30}))
        assert json.loads(ws.receive_text())["t"] == "o"
        ws.send_text(json.dumps({"t": "r", "c": 100, "r": 40}))
        ws.send_text(json.dumps({"t": "i", "d": "ls\r"}))
        assert ws.receive_bytes() == b"echo:ls\r"
        ws.send_text(json.dumps({"t": "i", "d": "exit\r"}))
        assert ws.receive_bytes() == b"echo:exit\r"
    assert fake_ssh["size"] == (500, 30)  # размер ограничен
    t = fake_ssh["target"]
    assert (t.username, t.host, t.port, t.password, t.key_path, t.label) == (
        "user", "192.0.2.10", 22, "hunter2", "/secret/key", "вручную")
    assert fake_ssh["process"].sizes == [(100, 40)]
    assert fake_ssh["conn"].closed and console._open == {}
    events = db.scalars(select(SecurityEvent).order_by(SecurityEvent.id)).all()
    assert [e.kind for e in events] == ["console_open", "console_close"]
    text = " ".join(e.detail for e in events)
    assert "user@192.0.2.10" in text and "hunter2" not in text and "ls\r" not in text and "echo:" not in text


def test_ticket_single_use_and_bound_to_server(admin, server, fake_ssh, db):
    ticket = _ticket(admin, server.id)
    other = Server(name="other", host="10.0.0.2", collector="ssh")
    db.add(other)
    db.commit()
    with admin.websocket_connect(f"/servers/{other.id}/console/ws", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        assert json.loads(ws.receive_text())["t"] == "e"
    with admin.websocket_connect(f"/servers/{server.id}/console/ws", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))  # уже сгорел
        assert json.loads(ws.receive_text())["t"] == "e"
    assert "target" not in fake_ssh


def test_ticket_bound_to_session(admin, server, fake_ssh):
    ticket = _ticket(admin, server.id)
    stranger = TestClient(app)  # без cookie сессии
    with stranger.websocket_connect(f"/servers/{server.id}/console/ws", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        assert json.loads(ws.receive_text())["t"] == "e"
    assert "target" not in fake_ssh


def test_ssh_error_shown_without_details(admin, server, monkeypatch, db):
    class HostKeyNotVerifiable(Exception):
        pass

    async def fail(*a, **kw):
        raise HostKeyNotVerifiable("/home/app/.ssh/known_hosts secret detail")

    monkeypatch.setattr(console, "open_shell", fail)
    console._open.clear()
    msg = _session(admin, server.id, {})
    assert msg["t"] == "e" and "изменился" in msg["m"] and "secret detail" not in msg["m"]
    ev = db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "console_open"))
    assert ev.ok is False and console._open == {}


def test_auth_failure_has_code(admin, server, monkeypatch):
    class PermissionDenied(Exception):
        pass

    async def deny(*a, **kw):
        raise PermissionDenied("Permission denied")

    monkeypatch.setattr(console, "open_shell", deny)
    console._open.clear()
    msg = _session(admin, server.id, {"cmd": "ssh user@192.0.2.10", "password": "wrong"})
    assert msg["t"] == "e" and msg["code"] == "auth" and "вход не удался" in msg["m"]
    assert "wrong" not in json.dumps(msg)


def test_other_errors_have_no_auth_code(admin, server, monkeypatch):
    async def refuse(*a, **kw):
        raise ConnectionRefusedError()

    monkeypatch.setattr(console, "open_shell", refuse)
    console._open.clear()
    assert "code" not in _session(admin, server.id, {})


def test_error_text_explains_network_failures():
    import errno
    import socket

    assert "закрыт" in ssh_access.error_text(ConnectionRefusedError())
    assert "маршрута" in ssh_access.error_text(OSError(errno.EHOSTUNREACH, "No route to host"))
    assert "не ответил" in ssh_access.error_text(TimeoutError())
    assert "не найден" in ssh_access.error_text(socket.gaierror())
    assert "OSError" in ssh_access.error_text(OSError(errno.EIO, "x"))


def test_connection_error_shows_address(admin, server, monkeypatch):
    async def refused(*a, **kw):
        raise ConnectionRefusedError()

    monkeypatch.setattr(console, "open_shell", refused)
    console._open.clear()
    msg = _session(admin, server.id, {})
    assert msg["t"] == "e" and msg["m"].startswith("user@192.0.2.10:22: порт SSH закрыт")


def test_limit_per_user(admin, server, fake_ssh):
    for _ in range(console.MAX_PER_USER):
        assert console.acquire(1)
    assert not console.acquire(1)
    console._open.clear()


def test_admin_group_gets_permission(db):
    g = db.scalar(select(Group).where(Group.name == "Администраторы"))
    assert "servers.console" in {p.perm for p in g.permissions}
    users = db.scalar(select(Group).where(Group.name == "Пользователи"))
    assert "servers.console" not in {p.perm for p in users.permissions}
    assert db.scalar(select(GroupPermission).where(GroupPermission.perm == "servers.console"))


# ---------- Сохранённые входы и ключи хостов ----------

def test_credentials_saved_encrypted_and_used(admin, server, fake_ssh, db):
    r = admin.post(f"/servers/{server.id}/credentials",
                   data={"kind": "bmc", "label": "iDRAC", "host": "192.0.2.11", "port": "22",
                         "username": "root", "password": "calvin-пароль"}, follow_redirects=False)
    assert r.status_code == 303
    cred = db.scalar(select(ServerCredential))
    assert cred.password_enc and "calvin" not in cred.password_enc and decrypt(cred.password_enc) == "calvin-пароль"
    for page in (admin.get(f"/servers/{server.id}/settings").text, admin.get(f"/servers/{server.id}/console").text):
        assert "calvin" not in page and "iDRAC" in page and cred.password_enc not in page
    # Консоль с сохранённым входом: свой адрес BMC, пароль из БД, ключа сервера у BMC нет
    assert _session(admin, server.id, {"cred": cred.id, "cmd": "ssh x@10.9.9.9", "password": "ignored"})["t"] == "o"
    t = fake_ssh["target"]
    assert (t.host, t.username, t.password, t.key_path) == ("192.0.2.11", "root", "calvin-пароль", "")
    log = " ".join(e.detail for e in db.scalars(select(SecurityEvent)))
    assert "calvin" not in log and "iDRAC" in log


def test_credential_of_other_server_rejected(admin, server, fake_ssh, db):
    other = Server(name="other", host="10.0.0.2", collector="ssh")
    db.add(other)
    db.flush()
    cred = ServerCredential(server_id=other.id, username="root", password_enc=encrypt("x"))
    db.add(cred)
    db.commit()
    assert _session(admin, server.id, {"cred": cred.id})["t"] == "e"
    assert admin.put(f"/servers/{server.id}/files?name=a&cred={cred.id}", content=b"x").status_code == 404
    assert "target" not in fake_ssh


KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


def _keys(db):
    db.expire_all()
    return {(k.host, k.port): k for k in db.scalars(select(SshHostKey))}


def test_host_key_remembered_and_reset(admin, server, fake_ssh, db):
    fake_ssh["new_key"] = KEY
    assert _session(admin, server.id, {})["t"] == "o"
    key = _keys(db)[("192.0.2.10", 22)]
    assert key.key_line == KEY
    # Отпечаток и сам ключ нигде не показываются: ни на странице, ни в журнале
    fp = ssh_access.fingerprint(KEY)
    page = admin.get(f"/servers/{server.id}").text
    assert fp.startswith("SHA256:") and fp not in page and "AAAAC3" not in page and "192.0.2.10:22" in page
    events = list(db.scalars(select(SecurityEvent)))
    assert any("ключ хоста запомнен" in e.detail for e in events) and not any(fp in e.detail for e in events)
    # Второе подключение — с запомненным ключом
    fake_ssh["new_key"] = None
    assert _session(admin, server.id, {})["t"] == "o" and fake_ssh["target"].host_key == KEY
    assert admin.post(f"/hostkeys/{key.id}/reset", follow_redirects=False).status_code == 303
    assert _keys(db) == {}
    assert any("сброшен ключ хоста" in e.detail for e in db.scalars(select(SecurityEvent)))


def test_keys_are_per_host_and_port(admin, server, fake_ssh, db):
    """Как known_hosts: на другом порту (другой sshd) и у BMC — свой ключ, чужой не подставляется."""
    db.add(SshHostKey(server_id=server.id, host="192.0.2.10", port=22, key_line="ssh-ed25519 OLD"))
    cred = ServerCredential(server_id=server.id, kind="bmc", host="192.0.2.11", username="root")
    db.add(cred)
    db.commit()
    fake_ssh["new_key"] = KEY
    assert _session(admin, server.id, {"cmd": "ssh -p 2222 user@192.0.2.10"})["t"] == "o"
    assert fake_ssh["target"].port == 2222 and fake_ssh["target"].host_key == ""
    assert _session(admin, server.id, {"cred": cred.id})["t"] == "o"
    assert fake_ssh["target"].host_key == "" and fake_ssh["target"].key_path == ""  # ключ сервера — не для BMC
    keys = _keys(db)
    assert keys[("192.0.2.10", 22)].key_line == "ssh-ed25519 OLD"
    assert keys[("192.0.2.10", 2222)].key_line == KEY and keys[("192.0.2.11", 22)].key_line == KEY


def test_manual_command_limited_to_server_addresses(admin, server, fake_ssh, db):
    msg = _session(admin, server.id, {"cmd": "ssh root@10.0.0.99"})
    assert msg["t"] == "e" and "не принадлежит серверу" in msg["m"]
    msg = _session(admin, server.id, {"cmd": "ssh -o ProxyCommand=x user@192.0.2.10"})
    assert msg["t"] == "e" and "-o" in msg["m"]
    assert "target" not in fake_ssh
    # Адрес сохранённого входа (BMC) — разрешён и вручную
    db.add(ServerCredential(server_id=server.id, kind="bmc", host="192.0.2.11", username="root"))
    db.commit()
    assert _session(admin, server.id, {"cmd": "ssh admin@192.0.2.11"})["t"] == "o"


@pytest.mark.parametrize("cmd, expected", [
    ("ssh user@192.0.2.10", ("user", "192.0.2.10", 22)),
    ("ssh -p 2222 user@192.0.2.10", ("user", "192.0.2.10", 2222)),
    ("-p2222 user@host", ("user", "host", 2222)),
    ("user@host", ("user", "host", 22)),
    ("host", ("def", "host", 22)),
    ("ssh://user@host:2200", ("user", "host", 2200)),
    ("ssh user@host -p 2022", ("user", "host", 2022)),
])
def test_parse_command(cmd, expected):
    assert ssh_access.parse_command(cmd, "def") == expected


@pytest.mark.parametrize("cmd", ["", "ssh", "ssh -p", "ssh -p x a@h", "ssh -p 0 a@h", "ssh -p 70000 a@h",
                                 "ssh -L 80:x:80 a@h", "ssh a@h b@h", "ssh 'a@h", "ssh a@h;rm", "ssh @h"])
def test_parse_command_rejects(cmd):
    with pytest.raises(ssh_access.BadTarget):
        ssh_access.parse_command(cmd, "")


def test_remote_path_is_home_only():
    from fastapi import HTTPException

    from app.routers.console import remote_path

    assert remote_path("a.txt") == "a.txt"  # относительно домашнего каталога SFTP
    for bad in ("../x", "a/b", "/etc/passwd", "..", "", "a\x00b", "a\\b"):
        with pytest.raises(HTTPException):
            remote_path(bad)


def test_page_has_placeholder_and_limit(admin, server):
    page = admin.get(f"/servers/{server.id}/console").text
    assert "console-placeholder" in page and "user@192.0.2.10:~$" in page
    assert "8 ГБ" in page and 'data-max="8589934592"' in page and 'name="dir"' not in page


def test_page_has_terminal_tabs(admin, server, db):
    page = admin.get(f"/servers/{server.id}/console").text
    assert 'id="term-tabs"' in page and 'id="term-add"' in page and 'data-max="10"' in page
    # Без кнопки «Подключиться»: подключается само; есть «Переподключить», «Загрузить файл» и окно логина
    assert "Подключиться" not in page and 'id="console-reconnect"' in page and 'id="console-upload"' in page
    assert '<dialog id="console-login"' in page and 'id="upload-form"' not in page
    assert '<template id="term-placeholder">' in page
    assert console.MAX_PER_USER == 10  # всего вкладок-консолей на пользователя
    # «Входы и ключи» — сразу в блок доступа настроек (сервер без узла — своя страница настроек)
    assert f'href="/servers/{server.id}/settings#access"' in page


def test_server_page_without_24h_chart(admin, server):
    page = admin.get(f"/servers/{server.id}").text
    assert "history-chart" not in page and "История за 24 часа" not in page


def test_upload_limit_capped_at_8gb():
    from app.services.settings_registry import SETTINGS

    s = next(s for s in SETTINGS if s.key == "console.max_upload_mb")
    assert s.default == 8192 and s.max == 8192


def test_missing_key_file_falls_back_to_password(sshd, tmp_path):
    """У сервера указан путь к ключу, которого нет в контейнере: с паролем — вход по паролю, без — понятная ошибка."""
    missing = str(tmp_path / "no-such-key")

    async def scenario():
        conn, _ = await ssh_access.connect(ssh_access.Target("127.0.0.1", sshd["port"], "u", "секрет", missing, "", "t"))
        conn.close()
        with pytest.raises(ssh_access.KeyFileUnavailable) as err:
            await ssh_access.connect(ssh_access.Target("127.0.0.1", sshd["port"], "u", "", missing, "", "t"))
        return ssh_access.error_text(err.value)

    assert "/home/app/.ssh" in asyncio.run(scenario())


def test_unreadable_ssh_config_ignored(sshd, tmp_path, monkeypatch):
    """~/.ssh в контейнере смонтирован с хоста: нечитаемый config не должен ломать вход."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host *\n  User other\n")
    (home / ".ssh" / "config").chmod(0)
    monkeypatch.setenv("HOME", str(home))

    async def scenario():
        conn, _ = await ssh_access.connect(ssh_access.Target("127.0.0.1", sshd["port"], "u", "секрет", "", "", "t"))
        conn.close()

    asyncio.run(scenario())


def test_inaccessible_ssh_dir_ignored(sshd, tmp_path, monkeypatch):
    """Регрессия: в Docker ~/.ssh (каталог ssh/ с хоста) был недоступен пользователю контейнера, а asyncssh по
    умолчанию проверяет ~/.ssh/crt (доверенные X.509) через Path.is_dir(), который ошибку доступа не глотает —
    «нет подключения (PermissionError: '/home/app/.ssh/crt')» на каждом входе."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh").chmod(0o600)  # без права входа в каталог: stat файлов внутри — EACCES
    monkeypatch.setenv("HOME", str(home))

    async def scenario():
        conn, _ = await ssh_access.connect(ssh_access.Target("127.0.0.1", sshd["port"], "u", "секрет", "", "", "t"))
        conn.close()

    try:
        asyncio.run(scenario())
    finally:
        (home / ".ssh").chmod(0o700)


def test_connect_without_local_username(sshd, monkeypatch):
    """В Docker у DASHBOARD_UID нет записи в /etc/passwd — asyncssh падал с ValueError («Unknown local username»)."""
    import pwd

    for var in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.delenv(var, raising=False)

    def no_user(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", no_user)

    async def scenario():
        conn, _ = await ssh_access.connect(ssh_access.Target("127.0.0.1", sshd["port"], "u", "секрет", "", "", "t"))
        conn.close()

    asyncio.run(scenario())


# ---------- Настоящий SSH/SFTP-сервер asyncssh на 127.0.0.1 ----------

@pytest.fixture
def sshd(tmp_path):
    """Сервер в своём потоке и цикле событий (TestClient крутит приложение в своём): пароль, shell, SFTP в chroot."""
    import asyncssh

    root = tmp_path / "root"
    root.mkdir()
    host_key = asyncssh.generate_private_key("ssh-ed25519")

    class Server_(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "u" and password == "секрет"

    async def shell(process):
        cols, rows = process.get_terminal_size()[:2]
        process.stdout.write(f"term={process.get_terminal_type()} {cols}x{rows}\r\n")
        line = await process.stdin.readline()
        process.stdout.write(f"got:{line.strip()}\r\n")
        process.exit(0)

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state = {}

    def run():
        asyncio.set_event_loop(loop)

        async def start():
            state["srv"] = await asyncssh.listen(
                "127.0.0.1", 0, server_host_keys=[host_key], server_factory=Server_, process_factory=shell,
                sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(root)))
            ready.set()

        loop.run_until_complete(start())
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(10)
    port = state["srv"].sockets[0].getsockname()[1]
    yield {"port": port, "root": root, "host_key": host_key.export_public_key().decode().strip()}
    loop.call_soon_threadsafe(state["srv"].close)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def test_open_shell_real_asyncssh_accept_new(sshd):
    async def scenario():
        target = ssh_access.Target("127.0.0.1", sshd["port"], "u", "секрет", "", "", "test")
        conn, process, new_key = await console.open_shell(target, 100, 30)
        assert new_key == sshd["host_key"]  # первое подключение — ключ получен с сервера
        out = b""
        while b"\n" not in out:
            out += await process.stdout.read(1024)
        process.stdin.write(b"hello\r")
        while b"got:" not in out:
            out += await process.stdout.read(1024)
        conn.close()
        # С запомненным ключом — без нового; с чужим ключом — отказ
        target.host_key = new_key
        conn, _, again = await console.open_shell(target, 80, 24)
        conn.close()
        target.host_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
        with pytest.raises(Exception) as err:
            await console.open_shell(target, 80, 24)
        return out, again, ssh_access.error_text(err.value)

    out, again, error = asyncio.run(scenario())
    assert b"term=xterm-256color 100x30" in out and b"got:hello" in out
    assert again is None and "изменился" in error


def test_file_upload_real_sftp(admin, db, sshd):
    srv = Server(name="lab", host="127.0.0.1", ssh_port=sshd["port"], collector="ssh")
    db.add(srv)
    db.flush()
    cred = ServerCredential(server_id=srv.id, username="u", port=sshd["port"], password_enc=encrypt("секрет"))
    db.add(cred)
    db.commit()
    # Каталог из запроса игнорируется: файл всегда в ~/ вошедшего пользователя (корень chroot тестового сервера)
    url = f"/servers/{srv.id}/files?cred={cred.id}&dir=/etc&name=hello.txt"
    r = admin.put(url, content=b"hello world")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "path": "~/hello.txt", "user": "u", "size": 11}
    assert (sshd["root"] / "hello.txt").read_bytes() == b"hello world"
    assert admin.put(url, content=b"again").status_code == 409  # без «заменить» не перезаписывает
    assert admin.put(url + "&overwrite=1", content=b"again").status_code == 200
    assert (sshd["root"] / "hello.txt").read_bytes() == b"again"
    assert admin.put(f"/servers/{srv.id}/files?cred={cred.id}&name=../a", content=b"x").status_code == 400
    assert _keys(db)[("127.0.0.1", sshd["port"])].key_line == sshd["host_key"]  # accept-new и для файлов
    # Вход вручную: команда ssh и пароль в заголовках (процентная кодировка), неверный пароль — понятная ошибка
    manual = f"/servers/{srv.id}/files?name=m.txt"
    cmd = f"ssh -p {sshd['port']} u@127.0.0.1"
    bad = admin.put(manual, content=b"x", headers={"X-SSH-Command": cmd, "X-SSH-Password": "wrong"})
    assert bad.status_code == 502 and "вход не удался" in bad.json()["detail"]
    foreign = admin.put(manual, content=b"x", headers={"X-SSH-Command": "ssh u@10.0.0.99", "X-SSH-Password": "x",
                                                       "Accept": "application/json"})  # как console.js
    assert foreign.status_code == 400 and "не принадлежит" in foreign.json()["detail"]
    ok = admin.put(manual, content=b"m", headers={"X-SSH-Command": cmd,
                                                  "X-SSH-Password": "%D1%81%D0%B5%D0%BA%D1%80%D0%B5%D1%82"})
    assert ok.status_code == 200 and (sshd["root"] / "m.txt").read_bytes() == b"m"
    events = db.scalars(select(SecurityEvent).where(SecurityEvent.kind == "file_upload")).all()
    assert len(events) == 5 and not any("секрет" in e.detail or "wrong" in e.detail for e in events)
    assert all("~/" in e.detail for e in events)


def test_file_upload_size_limit(admin, db, sshd):
    set_setting(db, "console.max_upload_mb", 1)
    srv = Server(name="lab", host="127.0.0.1", ssh_port=sshd["port"], collector="ssh")
    db.add(srv)
    db.flush()
    cred = ServerCredential(server_id=srv.id, username="u", port=sshd["port"], password_enc=encrypt("секрет"))
    db.add(cred)
    db.commit()
    url = f"/servers/{srv.id}/files?cred={cred.id}&name=big.bin"
    assert admin.put(url, content=b"x" * (2 * 1024 * 1024)).status_code == 413  # по Content-Length, до подключения

    def chunks():  # без Content-Length — обрыв на лету, недописанный файл удаляется
        for _ in range(3):
            yield b"x" * (512 * 1024)

    assert admin.put(url, content=chunks()).status_code == 413
    assert not (sshd["root"] / "big.bin").exists()
