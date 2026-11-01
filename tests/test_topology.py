from sqlalchemy import func, select

from app.models import Device, Group, Link, Server
from app.services.seed import seed


def test_seed_data_and_idempotent(db):
    first = seed(db)
    assert first == {"servers": 2, "devices": 11, "links": 10}
    assert seed(db) == {"servers": 0, "devices": 0, "links": 0}
    pi = db.scalar(select(Server).where(Server.name == "Raspberry Pi"))
    assert pi.collector == "ssh" and pi.enabled is False and pi.host == "192.168.1.10"
    mini = db.scalar(select(Server).where(Server.name == "Сервер (homelab)"))
    assert mini.collector == "local" and mini.enabled
    dev = db.scalar(select(Device).where(Device.name == "Сервер (homelab)"))
    assert dev.server_id == mini.id and dev.ip == "192.168.1.39"
    mikrotik = db.scalar(select(Device).where(Device.name == "MikroTik hEX S"))
    ports = {lk.a_port for lk in db.scalars(select(Link).where(Link.a_id == mikrotik.id))}
    assert ports == {"ether2", "ether3", "ether4"}


def test_cli_seed(db, capsys):
    from app.cli import main
    assert main(["seed"]) == 0
    assert "устройств 11" in capsys.readouterr().out
    assert db.scalar(select(func.count()).select_from(Device)) == 11


def test_api_topology_status_colors(admin, db):
    seed(db)
    mini = db.scalar(select(Server).where(Server.name == "Сервер (homelab)"))
    mini.online = True
    db.commit()
    data = admin.get("/api/topology").json()
    by_name = {n["name"]: n for n in data["nodes"]}
    assert by_name["Сервер (homelab)"]["status"] == "ok"
    assert by_name["Raspberry Pi"]["status"] == "unknown"  # выключен
    assert by_name["ПК"]["status"] == "none"
    assert len(data["edges"]) == 10 and data["editable"] is True
    assert admin.get("/topology").status_code == 200


def test_server_status_hidden_without_servers_view(viewer, db):
    seed(db)
    g = db.scalar(select(Group).where(Group.name == "Наблюдатели"))
    g.permissions = [p for p in g.permissions if p.perm != "servers.view"]
    db.commit()
    data = viewer.get("/api/topology").json()
    assert all(n["server"] is None and n["status"] == "none" for n in data["nodes"])
    assert data["editable"] is False


def test_save_positions(admin, viewer, db):
    seed(db)
    dev = db.scalar(select(Device).where(Device.name == "ПК"))
    body = {"positions": [{"id": dev.id, "x": 12.345, "y": -50}]}
    assert viewer.post("/api/topology/positions", json=body).status_code == 403
    assert admin.post("/api/topology/positions", json=body).json() == {"saved": 1}
    db.refresh(dev)
    assert (dev.x, dev.y) == (12.3, -50)
    assert admin.post("/api/topology/positions", json={"positions": [{"id": dev.id, "x": 1e9, "y": 0}]}).status_code == 422


def test_device_and_link_crud(admin, db):
    admin.post("/topology/devices", data={"name": "NAS", "type": "server", "ip": "192.168.1.40"})
    admin.post("/topology/devices", data={"name": "Switch", "type": "switch"})
    nas, sw = db.scalars(select(Device).order_by(Device.id)).all()
    assert admin.post("/topology/links", data={"a_id": nas.id, "b_id": nas.id}).status_code == 400
    admin.post("/topology/links", data={"a_id": nas.id, "a_port": "eth0", "b_id": sw.id, "b_port": "3", "label": "1G"})
    link = db.scalar(select(Link))
    assert (link.a_port, link.b_port, link.label) == ("eth0", "3", "1G")
    assert admin.post("/topology/devices", data={"name": "X", "type": "spaceship"}).status_code == 400
    admin.post(f"/topology/devices/{nas.id}", data={"name": "NAS2", "type": "server"})
    db.refresh(nas)
    assert nas.name == "NAS2" and nas.ip == ""
    admin.post(f"/topology/devices/{nas.id}/delete")
    assert db.scalar(select(Link)) is None  # связи удаляются вместе с устройством


def test_viewer_cannot_edit_topology(viewer, db):
    assert viewer.post("/topology/devices", data={"name": "X", "type": "pc"}).status_code == 403
    assert "Новое устройство" not in viewer.get("/topology").text


def test_server_delete_removes_its_device_and_links(admin, db):
    seed(db)
    mini = db.scalar(select(Server).where(Server.name == "Сервер (homelab)"))
    links_before = db.scalar(select(func.count()).select_from(Link))
    admin.post(f"/servers/{mini.id}/delete")
    db.expire_all()
    assert db.scalar(select(Device).where(Device.name == "Сервер (homelab)")) is None
    assert db.scalar(select(func.count()).select_from(Link)) == links_before - 1  # Свитч LAB — Mini PC
    assert db.scalar(select(Device).where(Device.name == "Свитч LAB")) is not None


def test_server_create_places_on_map(admin, db):
    switch = Device(name="Свитч LAB", type="switch")
    db.add(switch)
    db.commit()
    assert "Подключён к" in admin.get("/servers").text
    admin.post("/servers", data={"name": "NAS", "host": "192.168.1.40", "collector": "ssh", "on_map": "on",
                                 "uplink_id": str(switch.id)})
    server = db.scalar(select(Server).where(Server.name == "NAS"))
    device = db.scalar(select(Device).where(Device.server_id == server.id))
    assert device.type == "server" and device.ip == "192.168.1.40"
    link = db.scalar(select(Link).where(Link.b_id == device.id))
    assert link.a_id == switch.id
    assert "на карте сети: NAS" in admin.get(f"/servers/{server.id}").text
    # Без галочки — только сервер
    admin.post("/servers", data={"name": "Off", "host": "10.0.0.1", "collector": "ssh"})
    off = db.scalar(select(Server).where(Server.name == "Off"))
    assert db.scalar(select(Device).where(Device.server_id == off.id)) is None


def test_device_becomes_server(admin, db):
    admin.post("/topology/devices", data={"name": "Pi", "type": "server", "ip": "192.168.1.10", "make_server": "on"})
    device = db.scalar(select(Device).where(Device.name == "Pi"))
    assert device.server.host == "192.168.1.10" and device.server.collector == "ssh"
    assert device.server.enabled is False
    # Без IP сервер не создать
    r = admin.post("/topology/devices", data={"name": "NoIP", "type": "pc", "make_server": "on"})
    assert r.status_code == 400
    assert db.scalar(select(func.count()).select_from(Server)) == 1


def test_make_server_needs_servers_edit(viewer, db):
    assert "Добавить в «Серверы»" not in viewer.get("/topology").text
    assert viewer.post("/topology/devices", data={"name": "X", "type": "pc", "ip": "1.1.1.1",
                                                  "make_server": "on"}).status_code == 403
