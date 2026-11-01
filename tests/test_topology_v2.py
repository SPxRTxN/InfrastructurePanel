import re
from pathlib import Path

from sqlalchemy import select

from app.models import Device, Link, Segment
from app.services import ports as ports_svc
from app.services.seed import seed


def _dev(db, name, ports="", dtype="switch"):
    d = Device(name=name, type=dtype, ports=ports)
    db.add(d)
    db.commit()
    return d


def test_port_templates_and_parse():
    assert ports_svc.TEMPLATES["mikrotik_hex_s"]["ports"] == ["ether1", "ether2", "ether3", "ether4", "ether5", "sfp1"]
    assert ports_svc.TEMPLATES["isp_router"]["ports"] == ["LAN1", "LAN2", "LAN3", "LAN4", "WAN"]
    assert ports_svc.TEMPLATES["ap"]["ports"] == ["LAN", "Wi-Fi"]
    assert [len(ports_svc.TEMPLATES[k]["ports"]) for k in ("switch5", "switch8", "switch16", "switch24")] == [5, 8, 16, 24]
    assert ports_svc.parse_ports(" a, b\nb\n\n c ") == ["a", "b", "c"]


def test_device_form_template_and_manual_ports(admin, db):
    r = admin.post("/topology/devices", data={"name": "Свитч", "type": "switch", "ports_template": "switch5"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert db.scalar(select(Device)).ports.split("\n") == ["port1", "port2", "port3", "port4", "port5"]
    # ручной список главнее шаблона
    admin.post("/topology/devices", data={"name": "Р", "type": "router", "ports": "a\nb", "ports_template": "isp_router"})
    assert db.scalar(select(Device).where(Device.name == "Р")).ports == "a\nb"


def test_free_and_used_ports_in_api(admin, db):
    a, b = _dev(db, "A", "p1\np2"), _dev(db, "B", "q1")
    r = admin.post("/api/topology/links", json={"a_id": a.id, "b_id": b.id, "a_port": "p1", "b_port": "q1",
                                                "kind": "wired", "label": "1G"})
    assert r.status_code == 200 and r.json()["kind"] == "wired"
    nodes = {n["name"]: n for n in admin.get("/api/topology").json()["nodes"]}
    assert nodes["A"]["ports"] == [{"name": "p1", "used": True}, {"name": "p2", "used": False}]
    assert nodes["B"]["ports"] == [{"name": "q1", "used": True}]
    # занятый порт — 409
    c = _dev(db, "C", "c1")
    r = admin.post("/api/topology/links", json={"a_id": a.id, "b_id": c.id, "a_port": "p1", "b_port": "c1"})
    assert r.status_code == 409
    # свободный текст вне списка портов (старые связи) работает
    r = admin.post("/api/topology/links", json={"a_id": a.id, "b_id": c.id, "a_port": "ether9", "b_port": "x"})
    assert r.status_code == 200
    r = admin.post("/api/topology/links", json={"a_id": a.id, "b_id": a.id})
    assert r.status_code == 400
    assert admin.post("/api/topology/links", json={"a_id": a.id, "b_id": c.id, "kind": "bad"}).status_code == 400


def test_wifi_link_in_api_and_form(admin, db):
    ap, pc = _dev(db, "AP", "LAN\nWi-Fi", "ap"), _dev(db, "Ноут", "", "pc")
    r = admin.post("/topology/links", data={"a_id": pc.id, "b_id": ap.id, "kind": "wifi", "label": "5 ГГц"},
                   follow_redirects=False)
    assert r.status_code == 303
    edges = admin.get("/api/topology").json()["edges"]
    assert edges[0]["kind"] == "wifi" and edges[0]["label"] == "5 ГГц"
    # Wi-Fi порт не занимает; по умолчанию связь проводная
    admin.post("/topology/links", data={"a_id": pc.id, "b_id": ap.id})
    assert sorted(lk.kind for lk in db.scalars(select(Link))) == ["wifi", "wired"]


def test_link_delete_api(admin, db):
    a, b = _dev(db, "A"), _dev(db, "B")
    lid = admin.post("/api/topology/links", json={"a_id": a.id, "b_id": b.id}).json()["id"]
    assert admin.post(f"/api/topology/links/{lid}/delete").json() == {"ok": True}
    assert admin.post(f"/api/topology/links/{lid}/delete").status_code == 404


def test_segments_crud_and_move(admin, db):
    s = admin.post("/api/topology/segments", json={"name": "LAB", "color": "#ff0000", "subnet": "10.0.0.0/24"}).json()
    assert s["name"] == "LAB" and s["color"] == "#ff0000"
    assert admin.post("/api/topology/segments", json={"name": "x", "color": "red"}).status_code == 422
    assert admin.post(f"/api/topology/segments/{s['id']}", json={"name": "LAB2"}).json()["name"] == "LAB2"
    d = _dev(db, "Узел")
    r = admin.post(f"/api/topology/devices/{d.id}/segment", json={"segment_id": s["id"]})
    assert r.json()["segment_id"] == s["id"]
    data = admin.get("/api/topology").json()
    assert data["nodes"][0]["segment_id"] == s["id"] and data["segments"][0]["name"] == "LAB2"
    assert admin.post(f"/api/topology/devices/{d.id}/segment", json={"segment_id": 999}).status_code == 404
    assert admin.post(f"/api/topology/devices/{d.id}/segment", json={"segment_id": None}).json()["segment_id"] is None


def test_segment_delete_keeps_devices(admin, db):
    s = admin.post("/api/topology/segments", json={"name": "S"}).json()
    d = _dev(db, "Узел")
    admin.post(f"/api/topology/devices/{d.id}/segment", json={"segment_id": s["id"]})
    assert admin.post(f"/api/topology/segments/{s['id']}/delete").status_code == 200
    db.expire_all()
    assert db.get(Device, d.id).segment_id is None
    assert db.scalar(select(Segment)) is None
    assert admin.post(f"/api/topology/segments/{s['id']}/delete").status_code == 404


def test_device_form_segment(admin, db):
    s = admin.post("/api/topology/segments", json={"name": "S"}).json()
    admin.post("/topology/devices", data={"name": "Д", "type": "pc", "segment_id": str(s["id"])})
    assert db.scalar(select(Device)).segment_id == s["id"]
    assert admin.post("/topology/devices", data={"name": "Д2", "type": "pc", "segment_id": "999"}).status_code == 404


def test_viewer_cannot_edit(viewer, db):
    d = _dev(db, "A")
    for url, body in (("/api/topology/links", {"a_id": d.id, "b_id": d.id}),
                      ("/api/topology/segments", {"name": "S"}),
                      ("/api/topology/segments/1", {"name": "S"}),
                      ("/api/topology/segments/1/delete", None),
                      (f"/api/topology/devices/{d.id}/segment", {"segment_id": None}),
                      ("/api/topology/links/1/delete", None)):
        assert viewer.post(url, json=body).status_code == 403, url
    page = viewer.get("/topology")
    assert page.status_code == 200 and "topo-link-dialog" not in page.text
    assert viewer.get("/api/topology").json()["segments"] == []


def test_seed_still_works_with_v2_fields(admin, db):
    seed(db)
    data = admin.get("/api/topology").json()
    assert all(e["kind"] == "wired" for e in data["edges"])
    assert all(n["ports"] == [] and n["segment_id"] is None for n in data["nodes"])


def test_js_is_static_and_included(admin):
    page = admin.get("/topology").text
    assert "/static/js/topology.js" in page
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page)  # нет inline-скриптов
    assert not re.search(r"\son(click|change|submit|load)=", page)
    js = Path("app/static/js/topology.js").read_text(encoding="utf-8")
    assert "/api/topology/links" in js and "tapend" in js and "/segment`" in js
