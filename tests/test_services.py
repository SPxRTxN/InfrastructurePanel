"""Раздел «Сервисы»: права, CRUD, проверка веб-адреса, экранирование, поиск, модуль."""
from sqlalchemy import select

from app.models import Group, Server, Service


def _server(db, name="example-a", host="192.0.2.10") -> Server:
    s = Server(name=name, host=host, collector="local")
    db.add(s)
    db.commit()
    return s


def _form(server, **over) -> dict:
    data = {"server_id": server.id, "name": "example", "kind": "generic", "check_type": "tcp",
            "target": "192.0.2.10:80", "web_url": "", "description": ""}
    data.update(over)
    return data


def test_viewer_sees_but_cannot_change(admin, viewer, db):
    srv = _server(db)
    admin.post("/services", data=_form(srv))
    sv = db.scalar(select(Service))
    page = viewer.get("/services").text
    assert "example" in page and "Новый сервис" not in page
    assert viewer.get(f"/services/{sv.id}").status_code == 200
    assert viewer.post("/services", data=_form(srv)).status_code == 403
    assert viewer.post(f"/services/{sv.id}", data=_form(srv, name="x")).status_code == 403
    assert viewer.post(f"/services/{sv.id}/remove").status_code == 403


def test_module_disabled(admin, db):
    assert 'href="/services"' in admin.get("/").text
    admin.post("/settings/modules", data={"modules.enabled": ["servers"]})
    assert 'href="/services"' not in admin.get("/").text
    assert admin.get("/services").status_code == 403


def test_crud(admin, db):
    srv = _server(db)
    r = admin.post("/services", data=_form(srv, kind="filebrowser", web_url="https://192.0.2.10:8080",
                                           description="файлы"), follow_redirects=False)
    assert r.status_code == 303
    sv = db.scalar(select(Service))
    assert r.headers["location"] == f"/services/{sv.id}" and sv.kind == "filebrowser"
    page = admin.get(f"/services/{sv.id}").text
    assert f'href="/servers/{srv.id}"' in page and 'rel="noopener noreferrer"' in page
    assert admin.post(f"/services/{sv.id}", data=_form(srv, name="renamed", target="http://192.0.2.10/"
                                                         , check_type="http")).status_code == 200
    db.refresh(sv)
    assert (sv.name, sv.check_type, sv.ok) == ("renamed", "http", None)
    sid = sv.id
    admin.post(f"/services/{sid}/remove")
    db.expire_all()
    assert db.scalar(select(Service)) is None
    assert admin.get(f"/services/{sid}").status_code == 404
    assert "Сервисов пока нет" in admin.get("/services").text


def test_validation(admin, db):
    srv = _server(db)
    for bad in ("javascript:alert(1)", "//evil.example", "ftp://192.0.2.10"):
        assert admin.post("/services", data=_form(srv, web_url=bad)).status_code == 400, bad
    assert admin.post("/services", data=_form(srv, kind="nope")).status_code == 400
    assert admin.post("/services", data=_form(srv, target="no-port")).status_code == 400
    assert admin.post("/services", data=_form(srv, server_id=999)).status_code == 400
    assert admin.post("/services", data=_form(srv, name=" ")).status_code == 400
    assert db.scalar(select(Service)) is None


def test_xss_escaped(admin, db):
    srv = _server(db)
    evil = "<script>alert(1)</script>"
    admin.post("/services", data=_form(srv, name=evil, description=evil))
    sv = db.scalar(select(Service))
    for url in ("/services", f"/services/{sv.id}"):
        assert evil not in admin.get(url).text


def test_list_across_servers_and_search(admin, db):
    a, b = _server(db, "example-a"), _server(db, "example-b", "192.0.2.11")
    admin.post("/services", data=_form(a, name="alpha"))
    admin.post("/services", data=_form(b, name="beta"))
    page = admin.get("/services").text
    assert "alpha" in page and "beta" in page and "example-a" in page and "example-b" in page
    found = admin.get("/services", params={"q": "alp"}).text
    assert "alpha" in found and "beta" not in found
    assert "Сервисов пока нет" in admin.get("/services", params={"q": "zzz"}).text


def test_existing_groups_get_permissions_in_migration_logic(db):
    """Свежие группы по умолчанию содержат права раздела; Наблюдатели — только просмотр."""
    viewers = db.scalar(select(Group).where(Group.name == "Наблюдатели"))
    perms = {p.perm for p in viewers.permissions}
    assert "services.view" in perms and "services.edit" not in perms


def test_migration_0030_grants_rights_and_module(tmp_path):
    """Существующие группы и сохранённый список разделов получают «Сервисы»."""
    import json
    import sqlite3

    from alembic import command

    from app import migrate

    url = f"sqlite:///{tmp_path / 'old.db'}"
    command.upgrade(migrate.alembic_config(url), "0028_merge_recordings_kind")
    with sqlite3.connect(tmp_path / "old.db") as c:
        c.execute("INSERT INTO groups (id, name, description, builtin) VALUES (1, 'g', '', 0)")
        c.execute("INSERT INTO group_permissions (group_id, perm) VALUES (1, 'servers.view')")
        c.execute("INSERT INTO setting_values (key, user_id, value) VALUES ('modules.enabled', NULL, '[\"servers\"]')")
    command.upgrade(migrate.alembic_config(url), "0030_service_web")
    with sqlite3.connect(tmp_path / "old.db") as c:
        perms = {r[0] for r in c.execute("SELECT perm FROM group_permissions WHERE group_id = 1")}
        value = c.execute("SELECT value FROM setting_values WHERE key = 'modules.enabled'").fetchone()[0]
    assert perms == {"servers.view", "services.view"}
    assert json.loads(value) == ["servers", "services"]
