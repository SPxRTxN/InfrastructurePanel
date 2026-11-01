import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Server, ServerMetric, Service
from app.services import infra, monitoring
from app.utils import utcnow

FIXTURE = (Path(__file__).parent / "fixtures" / "ssh_output.txt").read_text()


def test_parse_ssh_output():
    m = monitoring.parse_ssh_output(FIXTURE)
    assert m.ram_total == 8245170176
    assert m.ram_free == 6312673280  # available
    assert m.ram_used == 8245170176 - 6312673280
    mounts = [d["mount"] for d in m.disks]
    assert mounts == ["/", "/boot/firmware", "/mnt/my data"]
    assert m.disks[2]["size"] == 500107862016


def test_parse_free_without_available():
    text = "             total       used       free\nMem:        1000        400        600\n"
    assert monitoring.parse_free(text) == (1000, 400, 600)


def test_collect_local_real_host():
    m = monitoring.collect_local()
    assert m.ram_total and m.ram_total >= m.ram_used
    assert m.disks


@pytest.fixture
def fake_network(monkeypatch):
    async def banner(host, port):
        return host != "10.0.0.99", "SSH-2.0-OpenSSH_9.6"

    async def run(server):
        return FIXTURE

    async def tcp(target):
        return target.endswith(":22"), ""

    monkeypatch.setattr(monitoring, "ssh_banner", banner)
    monkeypatch.setattr(monitoring, "run_ssh_command", run)
    monkeypatch.setattr(monitoring, "check_tcp", tcp)


def test_run_all_checks(db, fake_network):
    pi = Server(name="Pi", host="192.0.2.10", collector="ssh", ssh_user="pi")
    local = Server(name="Mini", host="127.0.0.1", collector="local")
    dead = Server(name="Dead", host="10.0.0.99", collector="ssh")
    off = Server(name="Off", host="10.0.0.1", collector="ssh", enabled=False)
    db.add_all([pi, local, dead, off])
    db.flush()
    db.add_all([Service(server_id=pi.id, name="ssh", check_type="tcp", target="192.0.2.10:22"),
                Service(server_id=pi.id, name="web", check_type="tcp", target="192.0.2.10:80")])
    db.add(ServerMetric(server_id=pi.id, online=True, ssh_ok=True, ts=utcnow() - timedelta(days=15)))
    db.add(ServerMetric(server_id=pi.id, online=True, ssh_ok=True, ts=utcnow() - timedelta(days=13)))
    db.commit()

    asyncio.run(monitoring.run_all_checks(db))
    db.expire_all()
    assert pi.online and pi.ssh_ok
    assert local.online
    assert dead.online is False and dead.last_error
    assert off.online is None and off.last_checked is None
    assert {s.name: s.ok for s in pi.services} == {"ssh": True, "web": False}
    m = monitoring.latest_metric(db, pi.id)
    assert m.ram_total == 8245170176 and len(json.loads(m.disks)) == 3
    # Старше history.retention_days (14) удаляет задача retention, моложе — остаётся (графики берут 24 ч)
    infra.cleanup(db)
    assert db.scalar(select(ServerMetric).where(ServerMetric.ts < utcnow() - timedelta(days=14))) is None
    assert db.scalar(select(ServerMetric).where(ServerMetric.ts < utcnow() - timedelta(days=12))) is not None


def _create(client, **extra):
    data = {"name": "Pi", "host": "192.0.2.10", "ssh_port": "22", "collector": "ssh", "ssh_user": "pi",
            "ssh_key_path": "/home/owner/.ssh/id_secret_key", "enabled": "on", **extra}
    return client.post("/servers", data=data, follow_redirects=False)


def test_key_path_encrypted_and_hidden(admin, db):
    assert _create(admin).status_code == 303
    server = db.scalar(select(Server))
    assert server.ssh_key_enc and "id_secret_key" not in server.ssh_key_enc
    for url in ["/servers", f"/servers/{server.id}", f"/servers/{server.id}/settings", "/api/servers", "/"]:
        r = admin.get(url)
        assert r.status_code == 200
        assert "id_secret_key" not in r.text and "ssh_key_enc" not in r.text, url
    assert admin.get("/api/servers").json()[0]["has_key"] is True


def test_empty_key_keeps_existing(admin, db):
    _create(admin)
    server = db.scalar(select(Server))
    enc = server.ssh_key_enc
    admin.post(f"/servers/{server.id}", data={"name": "Pi2", "host": "h", "collector": "ssh", "ssh_key_path": ""})
    db.expire_all()
    assert server.name == "Pi2" and server.ssh_key_enc == enc and server.enabled is False


def test_server_validation(admin):
    assert _create(admin, ssh_port="70000").status_code == 400
    assert _create(admin, collector="telnet").status_code == 400
    assert _create(admin, name="").status_code == 400


def test_server_permissions(viewer, user, db):
    assert _create(viewer).status_code == 403
    assert viewer.get("/servers").status_code == 200
    assert viewer.get("/api/servers").status_code == 200
    assert _create(user).status_code == 303
    sid = db.scalar(select(Server)).id
    assert viewer.post(f"/servers/{sid}/delete").status_code == 403
    assert viewer.post(f"/servers/{sid}/services", data={"name": "x", "check_type": "tcp", "target": "a:1"}).status_code == 403


def test_services_crud(admin, db):
    _create(admin)
    sid = db.scalar(select(Server)).id
    assert admin.post(f"/servers/{sid}/services", data={"name": "Web", "check_type": "http",
                                                        "target": "http://192.0.2.10"}, follow_redirects=False).status_code == 303
    assert admin.post(f"/servers/{sid}/services", data={"name": "Bad", "check_type": "http",
                                                        "target": "ftp://x"}).status_code == 400
    assert admin.post(f"/servers/{sid}/services", data={"name": "Bad", "check_type": "tcp",
                                                        "target": "nohostport"}).status_code == 400
    svc = db.scalar(select(Service))
    assert "Web" in admin.get(f"/servers/{sid}").text
    admin.post(f"/services/{svc.id}/delete")
    assert db.scalar(select(Service)) is None


def test_history_api_and_home_widget(admin, db):
    s = Server(name="Mini", host="127.0.0.1", collector="local", online=True)
    db.add(s)
    db.flush()
    db.add(ServerMetric(server_id=s.id, online=True, ssh_ok=False, ram_total=100, ram_used=40, ram_free=60,
                        disks=json.dumps([{"mount": "/", "size": 200, "used": 50, "free": 150}])))
    db.commit()
    hist = admin.get(f"/api/servers/{s.id}/history").json()
    assert hist[0]["ram_pct"] == 40.0 and hist[0]["disk_pct"] == 25.0
    html = admin.get("/").text
    assert "Mini" in html and "Серверов онлайн" in html and 'data-ring="40"' in html


def test_home_hides_servers_without_permission(viewer, db):
    from app.models import Group
    g = db.scalar(select(Group).where(Group.name == "Наблюдатели"))
    g.permissions = [p for p in g.permissions if p.perm != "servers.view"]
    db.commit()
    db.add(Server(name="Secret-Box", host="1.2.3.4"))
    db.commit()
    assert "Secret-Box" not in viewer.get("/").text
    assert viewer.get("/servers").status_code == 403


def test_delete_server_cascades(admin, db):
    _create(admin)
    sid = db.scalar(select(Server)).id
    db.add(ServerMetric(server_id=sid, online=True, ssh_ok=True))
    db.commit()
    admin.post(f"/servers/{sid}/delete")
    assert db.scalar(select(ServerMetric)) is None
