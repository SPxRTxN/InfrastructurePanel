"""Запись сеансов SSH-консоли: только вывод, лимит, режимы, права, скачивание, уборка, CSRF."""
import json
from datetime import timedelta

from sqlalchemy import select

from app.models import ConsoleRecording, SecurityEvent
from app.services import recording
from app.services.settings_registry import set_setting
from app.utils import utcnow
from tests.test_console import ORIGIN, _session, _ticket, fake_ssh, server  # noqa: F401  (фикстуры)

CMD = {"cmd": "ssh user@192.0.2.10", "password": "hunter2", "c": 80, "r": 24}


def _rec(db, server, **kw):
    r = recording.Recorder(80, 24, 1024)
    r.write("привет\r\n".encode())
    rid = r.save(db, server_id=server.id, server_name=server.name, user_id=None, username="admin",
                 target="user@192.0.2.10:22", reason="закрыта")
    row = db.get(ConsoleRecording, rid)
    for k, v in kw.items():
        setattr(row, k, v)
    db.commit()
    return rid


def _lines(db):
    row = db.scalar(select(ConsoleRecording))
    return [json.loads(x) for x in recording.cast_text(row).splitlines()]


def test_session_recorded_output_only(admin, server, fake_ssh, db):
    with admin.websocket_connect(f"/servers/{server.id}/console/ws", headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"ticket": _ticket(admin, server.id), **CMD}))
        assert json.loads(ws.receive_text())["t"] == "o"
        ws.send_text(json.dumps({"t": "r", "c": 100, "r": 40}))
        ws.send_text(json.dumps({"t": "i", "d": "sudo-password-xyz\r"}))
        ws.receive_bytes()
        ws.send_text(json.dumps({"t": "i", "d": "exit\r"}))
        ws.receive_bytes()
    lines = _lines(db)
    assert lines[0]["version"] == 2 and lines[0]["width"] == 80 and lines[0]["height"] == 24 and "timestamp" in lines[0]
    kinds = [e[1] for e in lines[1:]]
    assert "o" in kinds and "r" in kinds and set(kinds) <= {"o", "r"}  # события ввода "i" не пишутся
    assert [e[2] for e in lines[1:] if e[1] == "r"] == ["100x40"]
    row = db.scalar(select(ConsoleRecording))
    assert row.username == "admin" and row.server_id == server.id and row.target.endswith(":22")
    assert "hunter2" not in recording.cast_text(row) and "hunter2" not in row.target
    close = db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "console_close"))
    assert f"запись #{row.id}" in close.detail


def test_off_does_not_record(admin, server, fake_ssh, db):
    set_setting(db, "console.record", "off")
    _session(admin, server.id, {**CMD, "record": True})
    assert db.scalar(select(ConsoleRecording)) is None


def test_optional_follows_checkbox(admin, server, fake_ssh, db):
    set_setting(db, "console.record", "optional")
    _session(admin, server.id, {**CMD, "record": False})
    assert db.scalar(select(ConsoleRecording)) is None
    _session(admin, server.id, {**CMD, "record": True})
    assert len(db.scalars(select(ConsoleRecording)).all()) == 1


def test_always_ignores_checkbox(admin, server, fake_ssh, db):
    _session(admin, server.id, {**CMD, "record": False})
    assert len(db.scalars(select(ConsoleRecording)).all()) == 1


def test_limit_truncates():
    r = recording.Recorder(80, 24, 10)
    r.write(b"0123456789abcdef")
    r.write(b"more")
    assert r.truncated and r.size == 10
    texts = [e[2] for e in r.events]
    assert texts[0] == "0123456789" and "обрезана" in texts[1] and len(texts) == 2


def test_multibyte_split_across_chunks():
    r = recording.Recorder(80, 24, 100)
    data = "я".encode()
    r.write(data[:1])
    r.write(data[1:])
    assert "".join(e[2] for e in r.events) == "я"


def test_console_page_notice(admin, server, db):
    assert "Сеанс записывается" in admin.get(f"/servers/{server.id}/console").text
    set_setting(db, "console.record", "optional")
    page = admin.get(f"/servers/{server.id}/console").text
    assert "Сеанс записывается" in page and 'id="console-record"' in page
    set_setting(db, "console.record", "off")
    assert "Сеанс записывается" not in admin.get(f"/servers/{server.id}/console").text


def test_permissions(admin, user, viewer, server, db):
    rid = _rec(db, server)
    paths = ["/settings/recordings", f"/settings/recordings/{rid}", f"/settings/recordings/{rid}/cast"]
    for c in (user, viewer):
        for p in paths:
            assert c.get(p).status_code == 403
        assert c.post(f"/settings/recordings/{rid}/pin").status_code == 403
        assert c.post(f"/settings/recordings/{rid}/delete").status_code == 403
    for p in paths:
        assert admin.get(p).status_code == 200


def test_list_filter_by_server(admin, server, db):
    _rec(db, server)
    assert "привет" not in admin.get("/settings/recordings").text  # содержимое в список не попадает
    assert "<td>pi</td>" in admin.get(f"/settings/recordings?server={server.id}").text
    assert "Записей нет" in admin.get("/settings/recordings?server=99999").text


def test_download_cast_valid_and_audited(admin, server, db):
    rid = _rec(db, server)
    r = admin.get(f"/settings/recordings/{rid}/cast")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert json.loads(lines[0])["version"] == 2
    assert json.loads(lines[1])[1:] == ["o", "привет\r\n"]
    assert db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "recording_download")) is not None


def test_pin_and_delete_with_csrf(admin, server, db):
    rid = _rec(db, server)
    token = admin.headers.pop("X-CSRF-Token")
    assert admin.post(f"/settings/recordings/{rid}/pin").status_code == 403
    assert admin.post(f"/settings/recordings/{rid}/delete").status_code == 403
    admin.headers["X-CSRF-Token"] = token
    assert admin.post(f"/settings/recordings/{rid}/pin", follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(ConsoleRecording, rid).pinned is True
    admin.post(f"/settings/recordings/{rid}/pin")
    db.expire_all()
    assert db.get(ConsoleRecording, rid).pinned is False
    admin.post(f"/settings/recordings/{rid}/delete")
    db.expire_all()
    assert db.get(ConsoleRecording, rid) is None
    assert db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "recording_delete")) is not None


def test_cleanup_keeps_pinned_and_fresh(server, db):
    old = utcnow() - timedelta(days=40)
    gone, kept, fresh = _rec(db, server, ended_at=old), _rec(db, server, ended_at=old, pinned=True), _rec(db, server)
    recording.cleanup(db)
    db.expire_all()
    assert db.get(ConsoleRecording, gone) is None
    assert db.get(ConsoleRecording, kept) is not None and db.get(ConsoleRecording, fresh) is not None
