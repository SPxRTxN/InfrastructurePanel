"""Автоустановка агента: скрипт и экранирование, код (истечение, один токен), отчёты, права, SSH-ветка без сети."""
import asyncio
import hashlib
import json
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import AgentInstall, Node, SecurityEvent, Server, ServerCredential
from app.services import agent_install as svc
from app.services import agent_updates, ssh_access
from app.services.security import encrypt, hash_token
from app.utils import utcnow
from tests.test_agent_updates import make_archive


@pytest.fixture(autouse=True)
def release(tmp_path, monkeypatch, db):
    monkeypatch.setattr(agent_updates, "releases_dir", lambda: tmp_path / "agent-releases")
    data = make_archive("0.3.0")
    # start-agent в тестовом архиве не нужен: проверяется выдача файла и SHA
    return agent_updates.add_release(db, data, "", "тест", None), data


def make_server(db, name="example", node=True):
    server = Server(name=name, host="192.0.2.10", collector="ssh", ssh_user="user")
    db.add(server)
    db.commit()
    if node:
        n = Node(name="example", token_hash=hash_token("old"), server=server)
        db.add(n)
        db.commit()
    return server


def new_install(db, server=None, vpn=False, method="command"):
    server = server or make_server(db)
    node = db.scalar(select(Node).where(Node.server_id == server.id))
    inst, code = svc.create(db, node, None, vpn, method)
    return inst, code, node


def events(db, kind="agent_install"):
    db.expire_all()
    return db.scalars(select(SecurityEvent).where(SecurityEvent.kind == kind)).all()


# ---------- скрипт ----------

def test_script_served_without_session(client, db, release):
    _, code, node = new_install(db)
    r = client.get(f"/agent/install/{code}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-shellscript")
    body = r.text
    assert body.startswith("#!/usr/bin/env bash")
    assert f"NODE=example" in body and release[0].sha256 in body
    assert f"/agent/install/{code}/bundle" in body and "ws://testserver/agent/ws" in body
    assert "VPN=0" in body and code not in db.scalar(select(AgentInstall.code_hash))
    assert any("выдан скрипт" in e.detail for e in events(db))
    assert code not in "".join(e.detail for e in events(db))  # код в журнал не пишется


def test_script_quotes_values(db, release):
    server = make_server(db)
    node = db.scalar(select(Node))
    node.name = "x'; rm -rf / #"  # обход проверки имени — скрипт всё равно обязан экранировать
    text = svc.render_script("http://192.0.2.1:8000/", "code", node, release[0], True)
    assert "NODE='x'\"'\"'; rm -rf / #'" in text
    assert "VPN=1" in text and "{{" not in text and "{%" not in text


def test_script_vpn_flag(client, db):
    _, code, _ = new_install(db, vpn=True)
    assert "VPN=1" in client.get(f"/agent/install/{code}").text


def test_unknown_and_expired_code_is_plain_404(client, db):
    inst, code, _ = new_install(db)
    assert client.get("/agent/install/nope").status_code == 404
    inst.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    for path in ("", "/bundle"):
        r = client.get(f"/agent/install/{code}{path}")
        assert r.status_code == 404 and code not in r.text
    assert client.post(f"/agent/install/{code}/token").status_code == 404
    assert client.post(f"/agent/install/{code}/report", json={"step": "done", "status": "ok"}).status_code == 404
    assert any(not e.ok for e in events(db))


def test_code_stored_only_as_hash(db):
    inst, code, _ = new_install(db)
    assert inst.code_hash == hashlib.sha256(code.encode()).hexdigest() and code not in inst.code_hash
    assert (inst.expires_at - inst.created_at) == svc.CODE_TTL


def test_new_code_revokes_previous(client, db):
    server = make_server(db)
    _, old, _ = new_install(db, server)
    _, new, _ = new_install(db, server)
    assert client.get(f"/agent/install/{old}").status_code == 404
    assert client.get(f"/agent/install/{new}").status_code == 200


# ---------- bundle и токен ----------

def test_bundle_matches_sha(client, db, release):
    _, code, _ = new_install(db)
    r = client.get(f"/agent/install/{code}/bundle")
    assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == release[0].sha256
    assert r.content == release[1]


def test_token_issued_once(client, db):
    inst, code, node = new_install(db)
    old = node.token_hash
    r = client.post(f"/agent/install/{code}/token")
    assert r.status_code == 200
    token = r.json()["token"]
    db.expire_all()
    assert db.get(Node, node.id).token_hash == hash_token(token) != old
    assert client.post(f"/agent/install/{code}/token").status_code == 409
    assert all(token not in e.detail for e in events(db))


# ---------- отчёты ----------

def test_report_steps_and_done(client, db):
    inst, code, _ = new_install(db)
    url = f"/agent/install/{code}/report"
    assert client.post(url, json={"step": "python", "status": "running", "message": "ставлю"}).status_code == 200
    db.expire_all()
    assert db.get(AgentInstall, inst.id).status == "running"
    client.post(url, json={"step": "done", "status": "ok", "message": "готово", "log": "строка 1\nстрока 2"})
    db.expire_all()
    got = db.get(AgentInstall, inst.id)
    assert got.status == "ok" and got.finished_at and got.log == "строка 1\nстрока 2" and len(got.steps) == 2


def test_report_failure_and_validation(client, db):
    inst, code, _ = new_install(db)
    url = f"/agent/install/{code}/report"
    assert client.post(url, json={"step": "rm", "status": "ok"}).status_code == 400
    assert client.post(url, content=b"not json").status_code == 400
    assert client.post(url, json=[1]).status_code == 400
    client.post(url, json={"step": "token", "status": "failed", "message": "нет токена"})
    db.expire_all()
    assert db.get(AgentInstall, inst.id).status == "failed"


def test_report_limit_and_control_chars(client, db):
    inst, code, _ = new_install(db)
    url = f"/agent/install/{code}/report"
    big = json.dumps({"step": "done", "status": "ok", "log": "x" * 20000}).encode()
    assert client.post(url, content=big).status_code == 413
    dirty = {"step": "done", "status": "failed", "message": "a\x00b\x1b[31mred\x07", "log": "l\x1b[0m\x08\nok"}
    assert client.post(url, json=dirty).status_code == 200
    db.expire_all()
    got = db.get(AgentInstall, inst.id)
    assert got.steps[0]["message"] == "abred" and got.log == "l\nok"


def test_report_output_escaped_in_fragment(admin, client, db):
    inst, code, node = new_install(db)
    client.post(f"/agent/install/{code}/report",
                json={"step": "done", "status": "failed", "message": "<script>alert(1)</script>",
                      "log": "<img src=x onerror=alert(2)>"})
    r = admin.get(f"/servers/{node.server_id}/agent-install/progress")
    assert r.status_code == 200
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text
    assert "<img src=x" not in r.text and "&lt;img" in r.text
    assert "Что сделать" in r.text


def test_csrf_not_required_for_install_paths(client, db):
    # клиент без cookie и без X-CSRF-Token: обычный POST был бы 403
    _, code, _ = new_install(db)
    assert client.post(f"/agent/install/{code}/report", json={"step": "python", "status": "ok"}).status_code == 200
    assert client.post("/servers/1/agent-install").status_code == 403  # а обычные адреса CSRF по-прежнему требуют


# ---------- блок в настройках, права ----------

def test_fragment_and_loader(admin, db):
    server = make_server(db, node=False)
    page = admin.get(f"/servers/{server.id}/settings")
    assert page.status_code == 200 and f'hx-get="/servers/{server.id}/agent-install"' in page.text
    frag = admin.get(f"/servers/{server.id}/agent-install")
    assert 'id="agent"' in frag.text and "Получить команду" in frag.text and "Установить по SSH" in frag.text


def test_command_creates_node_and_shows_once(admin, db):
    server = make_server(db, name="Mini PC лаб", node=False)
    r = admin.post(f"/servers/{server.id}/agent-install")
    assert r.status_code == 200
    node = db.scalar(select(Node).where(Node.server_id == server.id))
    assert node and re.fullmatch(r"[A-Za-z0-9._-]+", node.name)
    code = re.search(r"agent/install/([A-Za-z0-9_-]+)", r.text).group(1)
    assert "| sudo bash" in r.text and r.headers["cache-control"] == "no-store"
    assert svc.by_code(db, code) is not None
    again = admin.get(f"/servers/{server.id}/agent-install")
    assert code not in again.text  # повторно код не показывается


def test_command_permissions(viewer, user, db):
    server = make_server(db)
    assert viewer.get(f"/servers/{server.id}/agent-install").status_code == 403
    assert viewer.post(f"/servers/{server.id}/agent-install").status_code == 403
    assert viewer.post(f"/servers/{server.id}/agent-install/ssh").status_code == 403
    assert viewer.get(f"/servers/{server.id}/agent-install/progress").status_code == 403


def test_vpn_flag_only_with_settings_manage(admin, user, db):
    server = make_server(db)
    user.post(f"/servers/{server.id}/agent-install", data={"vpn": "on"})
    assert db.scalars(select(AgentInstall)).all()[-1].vpn is False
    admin.post(f"/servers/{server.id}/agent-install", data={"vpn": "on"})
    db.expire_all()
    assert db.scalars(select(AgentInstall)).all()[-1].vpn is True


def test_token_page_has_command(admin, db):
    r = admin.post("/nodes", data={"name": "example2"})
    assert r.status_code == 200 and "| sudo bash" in r.text and "Установка вручную" in r.text


# ---------- SSH ----------

class FakeConn:
    def __init__(self, exit_status=0, out="установлено\n", err=""):
        self.calls, self.closed = [], False
        self.result = SimpleNamespace(stdout=out, stderr=err, exit_status=exit_status)

    async def run(self, cmd, input=None, check=False):
        self.calls.append((cmd, input))
        return self.result

    def close(self):
        self.closed = True


def patch_connect(monkeypatch, conn=None, error=None):
    seen = []

    async def fake(target):
        seen.append(target.address)
        if error:
            raise error
        return conn, None

    monkeypatch.setattr(ssh_access, "connect", fake)
    return seen


def test_ssh_command_password_only_in_stdin():
    cmd = svc.ssh_command_line("http://192.0.2.1/agent/install/c0de", False)
    assert cmd.startswith("sudo -S -p '' bash -c ") and "pa55" not in cmd
    assert svc.ssh_command_line("http://x/y", True).startswith("bash -c ")


def test_ssh_run_success_masks_password(monkeypatch, db):
    inst, code, node = new_install(db, method="ssh")
    conn = FakeConn(out="sudo ввёл pa55word\nготово", err="")
    patch_connect(monkeypatch, conn)
    target = ssh_access.Target("192.0.2.10", 22, "user", "", "", "", "сервер")
    asyncio.run(svc.run_ssh(inst.id, target, f"http://192.0.2.1/agent/install/{code}", "pa55word"))
    cmd, stdin = conn.calls[0]
    assert "pa55word" not in cmd and stdin == "pa55word\n" and conn.closed
    db.expire_all()
    got = db.get(AgentInstall, inst.id)
    assert got.status == "ok" and "pa55word" not in got.log and "********" in got.log
    assert "pa55word" not in got.steps_json


def test_ssh_run_failure_and_connect_error(monkeypatch, db):
    server = make_server(db)
    inst, code, _ = new_install(db, server, method="ssh")
    patch_connect(monkeypatch, FakeConn(exit_status=1, out="boom"))
    target = ssh_access.Target("192.0.2.10", 22, "root", "", "", "", "сервер")
    asyncio.run(svc.run_ssh(inst.id, target, "http://192.0.2.1/x", ""))
    db.expire_all()
    got = db.get(AgentInstall, inst.id)
    assert got.status == "failed" and "кодом 1" in got.steps[-1]["message"] and got.log == "boom"
    inst2, _, _ = new_install(db, server, method="ssh")
    patch_connect(monkeypatch, error=ConnectionRefusedError())
    asyncio.run(svc.run_ssh(inst2.id, target, "http://192.0.2.1/x", ""))
    db.expire_all()
    assert db.get(AgentInstall, inst2.id).status == "failed"
    assert "порт SSH закрыт" in db.get(AgentInstall, inst2.id).steps[-1]["message"]


def test_ssh_root_runs_without_sudo_and_stdin(monkeypatch, db):
    inst, _, _ = new_install(db, method="ssh")
    conn = FakeConn()
    patch_connect(monkeypatch, conn)
    asyncio.run(svc.run_ssh(inst.id, ssh_access.Target("192.0.2.10", 22, "root", "", "", "", "x"), "http://h/u", "pw"))
    assert conn.calls[0][0].startswith("bash -c ") and conn.calls[0][1] is None


def test_ssh_endpoint_starts_background_task(admin, db, monkeypatch):
    server = make_server(db)
    db.add(ServerCredential(server_id=server.id, kind="os", label="root", username="user", host="", port=22,
                            password_enc=encrypt("saved-pass")))
    db.commit()
    started = []
    monkeypatch.setattr(svc, "spawn", lambda *a: started.append(a))
    r = admin.post(f"/servers/{server.id}/agent-install/ssh", data={"sudo_password": "sudo-pass", "vpn": "on"})
    assert r.status_code == 200 and "идёт" in r.text and 'hx-trigger="every 2s"' in r.text
    inst_id, target, url, password = started[0]
    assert password == "sudo-pass" and url.startswith("http://testserver/agent/install/")
    inst = db.get(AgentInstall, inst_id)
    assert inst.method == "ssh" and inst.status == "running" and inst.vpn is True
    assert "sudo-pass" not in "".join(e.detail for e in events(db))
    assert "sudo-pass" not in r.text and "saved-pass" not in r.text


def test_ssh_endpoint_needs_console(user, db):
    server = make_server(db)  # у «Пользователей» нет servers.console
    assert user.post(f"/servers/{server.id}/agent-install/ssh").status_code == 403


def test_ssh_vpn_dropped_without_settings_manage(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Group, GroupPermission, User
    from app.services.security import hash_password
    from tests.conftest import PASSWORD, login

    group = Group(name="Установщики", permissions=[GroupPermission(perm=p) for p in
                                                   ("servers.view", "servers.edit", "servers.console")])
    db.add(User(username="installer", password_hash=hash_password(PASSWORD), groups=[group]))
    db.commit()
    client = login(TestClient(app), "installer")
    server = make_server(db)
    started = []
    monkeypatch.setattr(svc, "spawn", lambda *a: started.append(a))
    assert client.post(f"/servers/{server.id}/agent-install/ssh", data={"vpn": "on"}).status_code == 200
    assert db.get(AgentInstall, started[0][0]).vpn is False


def test_cleanup_removes_old(db):
    inst, _, _ = new_install(db)
    inst.expires_at = utcnow() - timedelta(days=2)
    db.commit()
    svc.cleanup(db)
    assert db.scalar(select(AgentInstall)) is None


def test_bundle_serves_version_from_script(admin, db, monkeypatch, tmp_path):
    """Скрипт вшивает SHA конкретной версии — архив по ?v= отдаётся ровно этой версии, даже если есть новее."""
    from app.models import AgentRelease
    from app.routers import agent_install as router_mod
    from app.services import agent_install as svc

    old, new = tmp_path / "old.tar.gz", tmp_path / "new.tar.gz"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    db.add_all([AgentRelease(version="0.1.0", sha256="a" * 64, size=3), AgentRelease(version="9.9.9", sha256="b" * 64, size=3)])
    db.commit()
    monkeypatch.setattr(svc, "release_file", lambda r: old if r.version == "0.1.0" else new)
    monkeypatch.setattr(router_mod.svc, "release_file", svc.release_file)
    node = db.scalar(select(Node).where(Node.name == "example")) or Node(name="example", token_hash="x" * 64)
    db.add(node)
    db.commit()
    _, code = svc.create(db, node, None, False, "command")
    assert admin.get(f"/agent/install/{code}/bundle?v=0.1.0").content == b"old"
    assert admin.get(f"/agent/install/{code}/bundle").content == b"new"  # без v — последняя


def test_progress_one_line_with_bar(admin, client, db):
    """Ход установки — одна строка с полосой: повторные сообщения шага не добавляют строк в основной вид."""
    inst, code, node = new_install(db)
    url = f"/agent/install/{code}/report"
    for step, status in (("download", "running"), ("download", "ok"), ("python", "running"), ("python", "ok"),
                         ("code", "running")):
        client.post(url, json={"step": step, "status": status, "message": f"m-{step}-{status}"})
    r = admin.get(f"/servers/{node.server_id}/agent-install/progress")
    line = r.text.split('class="install-progress-line"')[1].split("</div>")[0]
    assert "Шаг 3 из 7" in line and "m-code-running" in line and "m-python-ok" not in line
    assert 'max="7"' in r.text and 'value="2"' in r.text and "Все шаги" in r.text


# ---------- Windows ----------

def test_windows_script_served_and_quoted(client, db, release):
    _, code, node = new_install(db)
    r = client.get(f"/agent/install/{code}/windows")
    assert r.status_code == 200 and "charset=utf-8" in r.headers["content-type"]
    body = r.text
    assert "$Node = 'example'" in body and f"$Sha256 = '{release[0].sha256}'" in body
    assert f"/agent/install/{code}/bundle" in body and "'ws://testserver/agent/ws'" in body
    assert "-Unattended" not in body or "Unattended = $true" in body
    assert "{{" not in body and "{%" not in body
    node.name = "x'; Remove-Item C:\\ #"
    text = svc.render_windows("http://192.0.2.1:8000/", "code", node, release[0])
    assert "$Node = 'x''; Remove-Item C:\\ #'" in text  # ' удвоен — строка не разрывается


def test_command_shows_linux_and_windows(admin, db):
    server = make_server(db, node=False)
    r = admin.post(f"/servers/{server.id}/agent-install")
    code = re.search(r"agent/install/([A-Za-z0-9_-]+)", r.text).group(1)
    assert "| sudo bash" in r.text and f"irm &#39;http://testserver/agent/install/{code}/windows&#39; | iex" in r.text


def test_windows_installer_has_unattended_params():
    from pathlib import Path
    ps1 = (Path(__file__).resolve().parents[1] / "handoff/homelab-agent/start-agent.ps1").read_text(encoding="utf-8-sig")
    assert "param([string]$NodeName" in ps1 and "[switch]$Unattended" in ps1
    first = [ln for ln in ps1.splitlines() if ln.strip() and not ln.lstrip().startswith("#")][0]
    assert first.startswith("param(")  # param — первая инструкция скрипта, иначе PowerShell его не примет


def test_own_python_flag_reaches_script(admin, client, db, release):
    server = make_server(db, node=False)
    r = admin.post(f"/servers/{server.id}/agent-install", data={"own_python": "on"})
    code = re.search(r"agent/install/([A-Za-z0-9_-]+)", r.text).group(1)
    body = client.get(f"/agent/install/{code}").text
    assert "OWN_PYTHON=1" in body and "--own-python" in body
    r = admin.post(f"/servers/{server.id}/agent-install")
    code = re.search(r"agent/install/([A-Za-z0-9_-]+)", r.text).group(1)
    assert "OWN_PYTHON=0" in client.get(f"/agent/install/{code}").text


def test_new_server_auto_installs_agent_by_ssh(admin, db, monkeypatch):
    spawned = {}
    monkeypatch.setattr(svc, "spawn", lambda iid, target, url, sudo: spawned.update(t=target, sudo=sudo))
    r = admin.post("/servers", data={"name": "auto", "host": "192.0.2.60", "collector": "local", "ssh_port": "22",
                                     "ssh_user": "user", "install_agent": "on", "ssh_password": "pw1",
                                     "sudo_password": "", "own_python": "on"}, follow_redirects=False)
    from app.models import Server
    server = db.scalar(select(Server).where(Server.name == "auto"))
    node = db.scalar(select(Node).where(Node.server_id == server.id))
    assert r.status_code == 303 and r.headers["location"] == f"/nodes/{node.id}#agent"
    assert server.collector == "agent" and spawned["t"].host == "192.0.2.60" and spawned["t"].username == "user"
    assert spawned["sudo"] == "pw1"  # пусто — как пароль SSH
    inst = db.scalar(select(AgentInstall).where(AgentInstall.node_id == node.id))
    assert inst.method == "ssh" and inst.own_python and not inst.vpn
    assert "pw1" not in "".join(e.detail for e in events(db))


def test_new_server_without_install_and_callout(admin, db):
    r = admin.post("/servers", data={"name": "plain", "host": "192.0.2.61", "collector": "local", "ssh_port": "22"},
                   follow_redirects=False)
    assert r.status_code == 303 and "/nodes/" not in r.headers["location"]
    from app.models import Server
    server = db.scalar(select(Server).where(Server.name == "plain"))
    assert "Установить агента" in admin.get(f"/servers/{server.id}").text
    assert "install_agent" in admin.get("/servers").text
