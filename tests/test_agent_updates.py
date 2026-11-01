"""Обновление агентов из панели: каталог версий, выдача архива, страница и права, сообщение update, результат,
подтверждение по hello, откат, массовое добавление узлов, команда agent-release."""
import hashlib
import io
import json
import tarfile
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import cli
from app.models import AgentRelease, Node, NodeEvent, NodeUpdate
from app.services import agent_updates as svc
from app.services.security import hash_token
from app.utils import utcnow


def make_archive(version="0.2.0", prefix="", extra=None, init=None) -> bytes:
    files = {"homelab_agent/__init__.py": init if init is not None else f'__version__ = "{version}"\n'.encode(),
             "requirements.txt": b"psutil==1\n"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in {**files, **(extra or {})}.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def releases_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "releases_dir", lambda: tmp_path / "agent-releases")
    return tmp_path / "agent-releases"


def upload(client, data, version="", notes="", name="agent.tar.gz"):
    return client.post("/agents/releases", data={"version": version, "notes": notes},
                       files={"archive": (name, data, "application/gzip")}, follow_redirects=False)


# ---------- каталог версий ----------

def test_upload_stores_archive_and_computes_sha(admin, db, releases_dir):
    data = make_archive("0.2.0")
    assert upload(admin, data, version="0.2.0", notes="тест").status_code == 303
    r = db.scalar(select(AgentRelease))
    assert r.version == "0.2.0" and r.size == len(data) and r.notes == "тест" and r.uploaded_by
    assert r.sha256 == hashlib.sha256(data).hexdigest()
    assert (releases_dir / "0.2.0.tar.gz").read_bytes() == data
    assert (releases_dir / "0.2.0.tar.gz").stat().st_mode & 0o077 == 0
    assert "0.2.0" in admin.get("/agents").text


def test_upload_accepts_archive_inside_top_directory(admin, db):
    assert upload(admin, make_archive("0.3.1", prefix="homelab-agent/")).status_code == 303
    assert db.scalar(select(AgentRelease)).version == "0.3.1"


@pytest.mark.parametrize("data,msg", [
    (b"not a tarball", "не tar.gz"),
    (b"", "пуст"),
    (make_archive(init=b"x = 1\n"), "__version__"),
    (make_archive(init=b'__version__ = "../../etc"\n'), "некорректная версия"),
    (make_archive(extra={"../evil.py": b"x"}), "недопустимый"),
    (make_archive(extra={"/abs.py": b"x"}), "недопустимый"),
])
def test_upload_rejects_bad_archives(admin, db, releases_dir, data, msg):
    r = upload(admin, data)
    assert r.status_code == 400 and msg in r.text
    assert db.scalar(select(AgentRelease)) is None and not list(releases_dir.glob("*"))


def test_upload_rejects_archive_without_package(admin):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("readme.txt")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    assert "homelab_agent/__init__.py" in upload(admin, buf.getvalue()).text


def test_upload_version_mismatch_duplicate_and_limit(admin, db, monkeypatch):
    assert "не совпадает" in upload(admin, make_archive("0.2.0"), version="9.9.9").text
    assert upload(admin, make_archive("0.2.0")).status_code == 303
    assert "уже есть" in upload(admin, make_archive("0.2.0")).text
    monkeypatch.setattr(svc, "MAX_ARCHIVE", 100)
    assert "больше" in upload(admin, make_archive("0.4.0")).text


def test_upload_and_delete_need_settings_manage(user, viewer, db):
    for c in (user, viewer):
        assert upload(c, make_archive()).status_code == 403
        assert c.post("/agents/releases/1/delete").status_code == 403


def test_delete_release_removes_file(admin, db, releases_dir):
    upload(admin, make_archive())
    r = db.scalar(select(AgentRelease))
    assert admin.post(f"/agents/releases/{r.id}/delete", follow_redirects=False).status_code == 303
    assert db.scalar(select(AgentRelease)) is None and not (releases_dir / "0.2.0.tar.gz").exists()


# ---------- выдача архива агенту ----------

def make_node(db, features=("update",), name="pi", version="0.1.0"):
    token = "tok-" + name + "-" + "x" * 30
    n = Node(name=name, token_hash=hash_token(token), version=version, info_json=json.dumps({"features": list(features)}))
    db.add(n)
    db.commit()
    return n, token


def test_download_only_with_node_bearer_token(admin, client, db):
    data = make_archive()
    upload(admin, data)
    _, token = make_node(db)
    path = "/agent/releases/0.2.0.tar.gz"
    ok = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200 and ok.content == data
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong-token"}).status_code == 401
    assert client.get(path, headers={"Authorization": f"Basic {token}"}).status_code == 401
    # Сессия администратора токена узла не заменяет
    assert admin.get(path).status_code == 401
    # Токен верный, версии нет / имя странное
    for bad in ("/agent/releases/9.9.9.tar.gz", "/agent/releases/0.2.0.tar.gz.sig", "/agent/releases/x"):
        assert client.get(bad, headers={"Authorization": f"Bearer {token}"}).status_code == 404


def test_download_denied_after_token_reissue(admin, client, db):
    upload(admin, make_archive())
    n, token = make_node(db)
    admin.post(f"/nodes/{n.id}/token")
    assert client.get("/agent/releases/0.2.0.tar.gz", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ---------- страница и права ----------

def test_page_permissions(admin, user, viewer, client, db):
    upload(admin, make_archive())
    make_node(db)
    assert client.get("/agents", follow_redirects=False).status_code in (303, 401)
    for c in (admin, user, viewer):
        page = c.get("/agents")
        assert page.status_code == 200 and "0.2.0" in page.text and 'href="/agents"' in page.text
    assert 'action="/agents/releases"' in admin.get("/agents").text
    assert 'action="/agents/releases"' not in user.get("/agents").text
    assert "/agents/update" in user.get("/agents").text
    v = viewer.get("/agents").text
    assert "/agents/update" not in v and 'action="/agents/nodes"' not in v
    assert viewer.post("/agents/update", data={"release_id": 1, "node_id": 1}).status_code == 403
    assert viewer.post("/agents/nodes", data={"targets": "a u@h"}).status_code == 403


def test_page_shows_outdated_and_capability(admin, db):
    upload(admin, make_archive("0.2.0"))
    make_node(db, name="new", features=("update",), version="0.2.0")
    make_node(db, name="old", features=(), version="0.1.0")
    page = admin.get("/agents").text
    assert page.count("доступно обновление 0.2.0") == 1
    assert "не включено" in page


def test_version_order_is_numeric():
    assert svc.version_key("0.10.0") > svc.version_key("0.9.0")


# ---------- обновление узла по WebSocket ----------

def hello(token, version="0.1.0", features=("update",)):
    return json.dumps({"type": "hello", "v": 1, "node": "pi", "token": token, "version": version, "sensors": [],
                       "features": list(features)})


def wait_status(db, status, timeout=3):
    for _ in range(int(timeout / 0.05)):
        db.expire_all()
        upd = db.scalar(select(NodeUpdate).order_by(NodeUpdate.id.desc()))
        if upd and upd.status == status:
            return upd
        time.sleep(0.05)
    raise AssertionError(f"статус не стал {status}: {upd and upd.status}")


def test_update_message_result_and_confirmation(admin, db):
    upload(admin, make_archive("0.2.0"))
    release = db.scalar(select(AgentRelease))
    n, token = make_node(db)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            ws.send_text(hello(token))
            assert json.loads(ws.receive_text())["type"] == "welcome"
            r = admin.post("/agents/update", data={"release_id": release.id, "node_id": n.id}, follow_redirects=False)
            assert r.status_code == 303
            msg = json.loads(ws.receive_text())
            assert msg == {"type": "update", "id": msg["id"], "version": "0.2.0",
                           "url": "/agent/releases/0.2.0.tar.gz", "sha256": release.sha256}
            upd = db.scalar(select(NodeUpdate))
            assert upd.status == "sent" and upd.from_version == "0.1.0" and msg["id"] == f"u-{upd.id}"
            ws.send_text(json.dumps({"type": "update_result", "id": msg["id"], "ok": True,
                                     "detail": "версия включена", "version": "0.2.0"}))
            assert wait_status(db, "switching").detail == "версия включена"
        # Агент перезапустился и вернулся с новой версией
        with admin.websocket_connect("/agent/ws") as ws:
            ws.send_text(hello(token, version="0.2.0"))
            assert json.loads(ws.receive_text())["type"] == "welcome"
            assert "обновлено" in wait_status(db, "done").detail
    assert db.get(Node, n.id).version == "0.2.0"


def test_failed_result_is_logged_with_event(admin, db):
    upload(admin, make_archive("0.2.0"))
    release = db.scalar(select(AgentRelease))
    n, token = make_node(db)
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            ws.send_text(hello(token))
            ws.receive_text()
            admin.post(f"/agents/update/{n.id}", data={"release_id": release.id})
            msg = json.loads(ws.receive_text())
            ws.send_text(json.dumps({"type": "update_result", "id": msg["id"], "ok": False,
                                     "detail": "самотест новой версии не прошёл"}))
            assert "самотест" in wait_status(db, "failed").detail
    assert "не удалось" in db.scalar(select(NodeEvent)).text
    assert "самотест" in admin.get("/agents").text


def test_update_not_sent_to_node_without_feature(admin, db):
    upload(admin, make_archive("0.2.0"))
    release = db.scalar(select(AgentRelease))
    n, token = make_node(db, features=())
    with admin:
        with admin.websocket_connect("/agent/ws") as ws:
            ws.send_text(hello(token, features=()))
            ws.receive_text()
            admin.post("/agents/update", data={"release_id": release.id, "node_id": n.id})
            upd = db.scalar(select(NodeUpdate))
            assert upd.status == "failed" and "update" in upd.detail
            ws.send_text(json.dumps({"type": "event", "level": "info", "text": "ping"}))
            ws.close()


def test_offline_node_and_same_version_refused(admin, db):
    upload(admin, make_archive("0.2.0"))
    release = db.scalar(select(AgentRelease))
    off, _ = make_node(db, name="off")
    same, _ = make_node(db, name="same", version="0.2.0")
    admin.post("/agents/update", data={"release_id": release.id, "node_id": [off.id, same.id]})
    details = {u.node_id: u.detail for u in db.scalars(select(NodeUpdate))}
    assert "не на связи" in details[off.id] and "уже стоит" in details[same.id]
    assert admin.post("/agents/update", data={"release_id": release.id}).status_code == 400
    assert admin.post("/agents/update", data={"release_id": 999, "node_id": off.id}).status_code == 400


def test_update_result_of_other_node_is_ignored(admin, db):
    n, _ = make_node(db, name="a")
    other, token = make_node(db, name="pi")
    upd = NodeUpdate(node_id=n.id, from_version="0.1.0", to_version="0.2.0")
    db.add(upd)
    db.commit()
    svc.on_result(db, other, {"id": f"u-{upd.id}", "ok": True})
    db.refresh(upd)
    assert upd.status == "sent"


def test_unconfirmed_after_five_minutes_and_rollback(db):
    n, _ = make_node(db)
    upd = NodeUpdate(node_id=n.id, from_version="0.1.0", to_version="0.2.0", status="switching",
                     updated_at=utcnow() - timedelta(minutes=6))
    db.add(upd)
    db.commit()
    svc.expire(db)
    db.refresh(upd)
    assert upd.status == "unconfirmed" and "не подтвердилось" in upd.detail

    # Откат сторожем: агент вернулся со старой версией спустя минуту после ok
    upd2 = NodeUpdate(node_id=n.id, from_version="0.1.0", to_version="0.2.0", status="switching",
                      updated_at=utcnow() - timedelta(minutes=1))
    db.add(upd2)
    db.commit()
    svc.on_hello(db, n)  # n.version == 0.1.0 == from_version
    db.refresh(upd2)
    assert upd2.status == "rolled_back" and "откат" in upd2.detail

    # Тот же процесс переподключился сразу после ok — это ещё не откат
    upd3 = NodeUpdate(node_id=n.id, from_version="0.1.0", to_version="0.3.0", status="switching")
    db.add(upd3)
    db.commit()
    svc.on_hello(db, n)
    db.refresh(upd3)
    assert upd3.status == "switching"


def test_sent_without_answer_expires(db):
    n, _ = make_node(db)
    upd = NodeUpdate(node_id=n.id, from_version="0.1.0", to_version="0.2.0", requested_at=utcnow() - timedelta(minutes=30))
    db.add(upd)
    db.commit()
    svc.expire(db)
    db.refresh(upd)
    assert upd.status == "failed" and "нет ответа" in upd.detail


# ---------- массовое добавление узлов ----------

def test_bulk_add_nodes(admin, user, db):
    text = "raspberry-pi pi@192.168.1.10\n\n# комментарий\nnas admin@nas.lan\nbad name u@h\nnas dup@h\nx noat"
    page = user.post("/agents/nodes", data={"targets": text})
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    nodes = {n.name: n for n in db.scalars(select(Node))}
    assert set(nodes) == {"raspberry-pi", "nas"}
    assert "push-agent.sh pi@192.168.1.10 admin@nas.lan" in page.text
    for name in nodes:
        assert f'NODE="{name}"' in page.text
    # Каждый токен показан и подходит только к своему узлу; в БД — только хеш
    import re
    tokens = re.findall(r'TOKEN="([^"]+)"', page.text)
    assert len(tokens) == 2
    assert {hash_token(t) for t in tokens} == {n.token_hash for n in nodes.values()}
    assert all(t not in n.token_hash for t in tokens for n in nodes.values())
    assert "bad name" in page.text and "уже есть" in page.text and "noat" in page.text
    # Повтор — ничего нового, токенов нет
    again = admin.post("/agents/nodes", data={"targets": "nas admin@nas.lan"})
    assert 'TOKEN="' not in again.text and "уже есть" in again.text


def test_bulk_add_limit(admin, db):
    text = "\n".join(f"n{i} u@h{i}" for i in range(60))
    admin.post("/agents/nodes", data={"targets": text})
    assert len(db.scalars(select(Node)).all()) == svc.MAX_BULK


# ---------- CLI ----------

def test_cli_agent_release_add(db, tmp_path, capsys):
    f = tmp_path / "a.tar.gz"
    f.write_bytes(make_archive("1.2.3"))
    assert cli.main(["agent-release", "add", str(f), "--notes", "из CLI"]) == 0
    r = db.scalar(select(AgentRelease))
    assert r.version == "1.2.3" and r.notes == "из CLI" and r.uploaded_by is None
    assert r.sha256 in capsys.readouterr().out
    assert cli.main(["agent-release", "add", str(f)]) == 1  # дубликат
    bad = tmp_path / "b.tar.gz"
    bad.write_bytes(b"junk")
    assert cli.main(["agent-release", "add", str(bad)]) == 1
    assert cli.main(["agent-release", "add", str(tmp_path / "none")]) == 1
