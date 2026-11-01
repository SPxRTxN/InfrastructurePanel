"""SSH-консоль и файлы через агента: TCP-туннель к локальному sshd узла.

Агент - фейк в потоке теста: пересылает байты на настоящий asyncssh-сервер 127.0.0.1 (фикстура sshd из test_console).
"""
import base64
import json
import socket
import threading

import pytest
from sqlalchemy import select

from app.models import Node, Server, ServerCredential, SshHostKey
from app.services import agent_tunnel, console, ssh_access
from app.services.security import encrypt, hash_token
from tests.test_console import ORIGIN, sshd  # noqa: F401 - фикстура

TOKEN = "tok-" + "pi" * 10 + "x" * 20


class FakeAgent:
    """Читает WebSocket агента в потоке; tunnel_open -> TCP к sshd_port, байты туда-обратно."""

    def __init__(self, ws, port, refuse=None):
        self.ws, self.port, self.refuse = ws, port, refuse
        self.socks: dict[str, socket.socket] = {}
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def send(self, msg):
        with self.lock:
            self.ws.send_text(json.dumps(msg))

    def pump(self, tid, sock):
        try:
            while data := sock.recv(32768):
                self.send({"type": "tunnel_data", "id": tid, "d": base64.b64encode(data).decode()})
            self.send({"type": "tunnel_close", "id": tid, "reason": "closed by host"})
        except Exception:  # noqa: BLE001 - сокет закрыт при остановке теста
            pass

    def serve(self):
        while True:
            try:
                msg = json.loads(self.ws.receive_text())
            except Exception:  # noqa: BLE001 - панель закрыла соединение
                break
            tid, kind = msg.get("id"), msg["type"]
            if kind == "tunnel_open":
                self.opened.append(tid)
                if self.refuse:
                    self.send({"type": "tunnel_opened", "id": tid, "ok": False, "detail": self.refuse})
                    continue
                sock = socket.create_connection(("127.0.0.1", self.port))
                self.socks[tid] = sock
                self.send({"type": "tunnel_opened", "id": tid, "ok": True})
                threading.Thread(target=self.pump, args=(tid, sock), daemon=True).start()
            elif kind == "tunnel_data" and tid in self.socks:
                self.socks[tid].sendall(base64.b64decode(msg["d"]))
            elif kind == "tunnel_close":
                self.closed.append(tid)
                sock = self.socks.pop(tid, None)
                if sock:
                    sock.close()
        for sock in self.socks.values():
            sock.close()


def _setup(db, sshd, features=("tunnel",)):  # noqa: F811
    srv = Server(name="lab", host="10.255.255.1", ssh_port=2222, collector="ssh")  # напрямую недостижим
    db.add(srv)
    db.flush()
    db.add(Node(name="pi", token_hash=hash_token(TOKEN), server_id=srv.id,
                info_json=json.dumps({"features": list(features)})))
    cred = ServerCredential(server_id=srv.id, username="u", port=22, password_enc=encrypt("секрет"))
    db.add(cred)
    db.commit()
    return srv, cred


def _hello(ws, features=("tunnel",), tmax=2):
    ws.send_text(json.dumps({"type": "hello", "v": 1, "node": "pi", "token": TOKEN, "sensors": [],
                             "features": list(features), "tunnel_max": tmax}))
    assert json.loads(ws.receive_text())["type"] == "welcome"


def _put(admin, srv, cred, name="a.txt", content=b"data", via="agent"):
    return admin.put(f"/servers/{srv.id}/files?cred={cred.id}&name={name}", content=content,
                     headers={"X-SSH-Via": via, "Accept": "application/json"})


def test_sftp_upload_via_agent(admin, db, sshd):  # noqa: F811
    srv, cred = _setup(db, sshd)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws)
            agent = FakeAgent(ws, sshd["port"])
            r = _put(admin, srv, cred, content=b"hello via agent")
            assert r.status_code == 200, r.text
            assert (sshd["root"] / "a.txt").read_bytes() == b"hello via agent"
            # Ключ хоста - отдельно от прямого: host=agent:<id>, port=22; первый вход открыл пробу и сам сеанс
            keys = db.scalars(select(SshHostKey)).all()
            assert [(k.host, k.port, k.key_line) for k in keys] == [(f"agent:{agent_node_id(db)}", 22, sshd["host_key"])]
            assert len(agent.opened) == 2
            r2 = _put(admin, srv, cred, name="b.txt")  # ключ известен - один туннель
            assert r2.status_code == 200 and len(agent.opened) == 3
            assert _put(admin, srv, cred, name="b.txt").status_code == 409
    assert set(agent.closed) >= set(agent.opened[:1])  # закрытие со стороны панели дошло до агента


def agent_node_id(db):
    return db.scalar(select(Node.id))


def test_console_via_agent(admin, db, sshd):  # noqa: F811
    srv, cred = _setup(db, sshd)
    console._tickets.clear()
    console._open.clear()
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws)
            FakeAgent(ws, sshd["port"])
            ticket = admin.post(f"/servers/{srv.id}/console/ticket").json()["ticket"]
            with admin.websocket_connect(f"/servers/{srv.id}/console/ws", headers=ORIGIN) as term:
                term.send_text(json.dumps({"ticket": ticket, "via": "agent", "cred": cred.id, "c": 90, "r": 20}))
                assert json.loads(term.receive_text())["t"] == "o"
                out = b""
                while b"term=xterm-256color 90x20" not in out:
                    out += term.receive_bytes()
                term.send_text(json.dumps({"t": "i", "d": "ping\r"}))
                while b"got:ping" not in out:
                    out += term.receive_bytes()


def test_manual_command_address_ignored_via_agent(admin, db, sshd):  # noqa: F811
    srv, _ = _setup(db, sshd)
    t = ssh_access.resolve(srv, None, "ssh -p 2200 u@8.8.8.8", "pw", via_node=7)
    assert (t.host, t.port, t.username, t.node_id) == ("agent:7", 22, "u", 7)
    bmc = ServerCredential(server_id=srv.id, kind="bmc", username="admin", host="10.0.0.5", port=22)
    with pytest.raises(ssh_access.BadTarget):
        ssh_access.resolve(srv, bmc, via_node=7)


def test_busy_message(admin, db, sshd):  # noqa: F811
    srv, cred = _setup(db, sshd)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws, tmax=2)
            FakeAgent(ws, sshd["port"], refuse="busy")
            r = _put(admin, srv, cred)
    assert r.status_code == 502 and "агент занят: открыто максимум сеансов (2)" in r.json()["detail"]


def test_agent_refusals_are_explained(admin, db, sshd):  # noqa: F811
    srv, cred = _setup(db, sshd)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws)
            FakeAgent(ws, sshd["port"], refuse="disabled")
            assert "[terminal] enabled = false" in _put(admin, srv, cred).json()["detail"]


def test_agent_without_feature_or_offline(admin, db, sshd):  # noqa: F811
    srv, cred = _setup(db, sshd)
    r = _put(admin, srv, cred)  # агента нет в сети
    assert r.status_code == 400 and "агент не в сети" in r.json()["detail"]
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws, features=())  # онлайн, но без tunnel
            r = _put(admin, srv, cred)
            assert r.status_code == 400 and "[terminal] enabled = false" in r.json()["detail"]
            page = admin.get(f"/servers/{srv.id}/console")
            assert "console-via" not in page.text  # переключателя нет
    # и через WS консоли - понятная ошибка, без падения
    console._tickets.clear()
    with admin:
        ticket = admin.post(f"/servers/{srv.id}/console/ticket").json()["ticket"]
        with admin.websocket_connect(f"/servers/{srv.id}/console/ws", headers=ORIGIN) as term:
            term.send_text(json.dumps({"ticket": ticket, "via": "agent", "cred": cred.id, "c": 80, "r": 24}))
            msg = json.loads(term.receive_text())
    assert msg["t"] == "e" and "агент не в сети" in msg["m"]


def test_toggle_shown_when_agent_online_with_tunnel(admin, db, sshd):  # noqa: F811
    srv, _ = _setup(db, sshd)
    assert "console-via" not in admin.get(f"/servers/{srv.id}/console").text
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws)
            html = admin.get(f"/servers/{srv.id}/console").text
    assert "console-via" in html and "Через агента «pi»" in html


def test_agent_disconnect_closes_tunnel_and_pending(admin, db, sshd):  # noqa: F811
    _setup(db, sshd)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws)
            agent_tunnel.on_message(1, {"type": "tunnel_close", "id": "t-none"})  # неизвестный id - игнор
    assert not agent_tunnel._links and not agent_tunnel._pending


def test_hello_keeps_tunnel_max(admin, db, sshd):  # noqa: F811
    _setup(db, sshd)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            _hello(ws, tmax=3)
            db.expire_all()
            assert db.scalar(select(Node)).info["tunnel_max"] == 3
