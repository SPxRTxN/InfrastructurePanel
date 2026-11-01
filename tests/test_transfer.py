"""Перенос конфигурации: экспорт → импорт, пароль, конфликты, секреты, права, откат."""
import base64
import json
import re

import pytest
from sqlalchemy import delete, select

from app.models import (AlertRule, Device, Group, GroupPermission, Link, Node, SecurityEvent, Server, Service,
                        SettingValue, User)
from app.services import transfer
from app.services.security import decrypt, encrypt, hash_token, verify_password
from app.services.settings_registry import get_setting, set_setting

PW = "package-pass-1"
ALL = set(transfer.SECTIONS)


@pytest.fixture
def world(db):
    """Свои сущности (всё, что кроме тестовых admin/user/viewer и стандартных групп)."""
    group = Group(name="Тест-группа", description="для переноса", permissions=[GroupPermission(perm="jira.view")])
    server = Server(name="Pi", host="192.168.1.10", collector="ssh", ssh_user="pi", ssh_port=2222,
                    ssh_key_enc=encrypt("/home/secret/id_ed25519"), enabled=False)
    db.add_all([group, server])
    db.flush()
    db.add(Service(server_id=server.id, name="ssh", check_type="tcp", target="192.168.1.10:22"))
    db.add(User(username="bob", display_name="Боб", password_hash="$argon2id$fake-bob-hash", is_active=False,
                groups=[group]))
    a, b = Device(name="Свитч", type="switch", x=10.5, y=20.0), Device(name="Pi", type="server", server_id=server.id)
    db.add_all([a, b])
    db.flush()
    db.add_all([Link(a_id=a.id, a_port="p1", b_id=b.id, b_port="eth0", label="кабель"),
                Node(name="pi", token_hash=hash_token("tok-pi"), server_id=server.id),
                AlertRule(name="Диск почти полон", kind="disk", threshold=91.0, for_minutes=7, severity="critical")])
    db.commit()
    set_setting(db, "appearance.accent", "violet" if get_setting(db, "appearance.accent") != "violet" else "blue")
    set_setting(db, "alerts.telegram_token", "SECRET-BOT-TOKEN")
    set_setting(db, "alerts.silence_until", "2099-01-01T00:00:00")
    set_setting(db, "alerts.webhook_url", "https://ntfy.example/secret-topic")


def wipe_world(db):
    db.execute(delete(Link))
    db.execute(delete(Device))
    db.execute(delete(Node))
    db.execute(delete(Service))
    db.execute(delete(Server))
    db.execute(delete(AlertRule))
    db.execute(delete(User).where(User.username == "bob"))
    db.execute(delete(Group).where(Group.name == "Тест-группа"))
    db.execute(delete(SettingValue).where(SettingValue.key == "appearance.accent"))
    db.commit()
    db.expire_all()


def package_text(db, **kw) -> str:
    return json.dumps(transfer.build_package(db, ALL, **kw), ensure_ascii=False)


def test_roundtrip_restores_everything(db, world):
    accent = get_setting(db, "appearance.accent")
    blob = transfer.seal(transfer.build_package(db, ALL, passwords=True, same_tokens=True), PW)
    wipe_world(db)
    package = transfer.open_package(blob, PW)
    report = transfer.apply(db, package, {s: "skip" for s in package["sections"]})
    assert not report["tokens"]  # те же токены: новых не выдавали

    assert get_setting(db, "appearance.accent") == accent
    group = db.scalar(select(Group).where(Group.name == "Тест-группа"))
    assert [p.perm for p in group.permissions] == ["jira.view"]
    bob = db.scalar(select(User).where(User.username == "bob"))
    assert (bob.display_name, bob.is_active, bob.password_hash) == ("Боб", False, "$argon2id$fake-bob-hash")
    assert [g.name for g in bob.groups] == ["Тест-группа"]
    server = db.scalar(select(Server).where(Server.name == "Pi"))
    assert (server.host, server.ssh_port, server.ssh_user, server.enabled) == ("192.168.1.10", 2222, "pi", False)
    assert decrypt(server.ssh_key_enc) == "/home/secret/id_ed25519"
    assert [(s.name, s.target) for s in server.services] == [("ssh", "192.168.1.10:22")]
    switch = db.scalar(select(Device).where(Device.name == "Свитч"))
    assert (switch.x, switch.y) == (10.5, 20.0)
    assert db.scalar(select(Device).where(Device.name == "Pi")).server_id == server.id
    link = db.scalar(select(Link))
    assert (link.a_port, link.b_port, link.label) == ("p1", "eth0", "кабель")
    node = db.scalar(select(Node))
    assert node.token_hash == hash_token("tok-pi") and node.server_id == server.id
    rule = db.scalar(select(AlertRule))
    assert (rule.threshold, rule.for_minutes, rule.severity) == (91.0, 7, "critical")


def test_secret_key_reencrypted_for_this_panel(db, world):
    plain = transfer.build_package(db, {"servers"})["sections"]["servers"][0]["ssh_key"]
    assert plain == "/home/secret/id_ed25519"  # внутри шифрования пакета путь открытый, в БД — Fernet
    assert "/home/secret" not in db.scalar(select(Server.ssh_key_enc))


def test_wrong_password_and_garbage(db, world):
    blob = transfer.seal(transfer.build_package(db, {"settings"}), PW)
    with pytest.raises(transfer.TransferError, match="неверный пароль"):
        transfer.open_package(blob, "other-password")
    with pytest.raises(transfer.TransferError, match="неверный пароль"):
        transfer.open_package(b"not an age file", PW)
    with pytest.raises(transfer.TransferError, match="не короче"):
        transfer.seal({}, "short")
    with pytest.raises(transfer.TransferError, match="не конфигурация|Это не пакет"):
        transfer.open_package(transfer.age.encrypt(b'{"format": "other"}', PW), PW)
    with pytest.raises(transfer.TransferError, match="Версия"):
        transfer.open_package(transfer.age.encrypt(b'{"format": "homelab-config", "version": 9}', PW), PW)


def test_no_secrets_without_checkboxes(db, world):
    text = package_text(db)  # без паролей и токенов
    for secret in ("fake-bob-hash", hash_token("tok-pi"), "SECRET-BOT-TOKEN", "secret-topic", "2099-01-01"):
        assert secret not in text
    assert "password_hash" not in text and "token_hash" not in text
    with_pw = package_text(db, passwords=True, same_tokens=True)
    assert "fake-bob-hash" in with_pw and hash_token("tok-pi") in with_pw
    # каналы оповещений и тишина не уезжают даже с галочками
    assert "SECRET-BOT-TOKEN" not in with_pw and "secret-topic" not in with_pw and "2099-01-01" not in with_pw
    # шифрованный файл не содержит открытого текста
    assert "Свитч".encode() not in transfer.seal(json.loads(text), PW)


def test_import_without_password_and_token_issues_new(db, world):
    blob = transfer.seal(transfer.build_package(db, {"users", "groups", "servers", "nodes"}), PW)
    wipe_world(db)
    package = transfer.open_package(blob, PW)
    info = transfer.preview(db, package)
    assert any("Пароли не перенесены" in w for w in info["warnings"])
    report = transfer.apply(db, package, {s: "skip" for s in package["sections"]})
    bob = db.scalar(select(User).where(User.username == "bob"))
    assert bob.password_hash != "$argon2id$fake-bob-hash" and not verify_password(bob.password_hash, "bob")
    token = report["tokens"]["pi"]
    assert db.scalar(select(Node)).token_hash == hash_token(token)


def test_preview_and_both_strategies(db, world):
    blob = transfer.seal(transfer.build_package(db, {"servers", "alerts", "settings"}), PW)
    # Меняем после экспорта: сервер — конфликт, правило — совпадает, настройки — новая строка
    server = db.scalar(select(Server))
    server.host = "10.0.0.5"
    db.add(Server(name="Лишний", host="1.1.1.1"))
    db.execute(delete(SettingValue).where(SettingValue.key == "appearance.accent"))
    db.commit()
    package = transfer.open_package(blob, PW)
    info = transfer.preview(db, package)["sections"]
    assert (info["servers"]["new"], info["servers"]["same"], info["servers"]["conflict"]) == (0, 0, 1)
    assert (info["alerts"]["same"], info["alerts"]["conflict"]) == (1, 0)
    assert info["settings"]["new"] >= 1

    report = transfer.apply(db, package, {"servers": "skip", "alerts": "skip", "settings": "skip"})
    db.expire_all()
    assert report["counts"]["servers"]["skipped"] == 1
    assert db.scalar(select(Server).where(Server.name == "Pi")).host == "10.0.0.5"
    assert db.scalar(select(SettingValue).where(SettingValue.key == "appearance.accent")) is not None

    report = transfer.apply(db, package, {"servers": "replace"})
    db.expire_all()
    assert report["counts"]["servers"]["replaced"] == 1
    assert db.scalar(select(Server).where(Server.name == "Pi")).host == "192.168.1.10"
    assert db.scalar(select(Server).where(Server.name == "Лишний")) is not None  # replace не удаляет лишнее


def test_failure_rolls_back_everything(db, world, monkeypatch):
    blob = transfer.seal(transfer.build_package(db, {"settings", "groups", "alerts"}), PW)
    wipe_world(db)
    package = transfer.open_package(blob, PW)

    def boom(*a, **kw):
        raise RuntimeError("сбой")

    monkeypatch.setitem(transfer._APPLY, "alerts", boom)
    with pytest.raises(transfer.TransferError, match="ничего не изменено"):
        transfer.apply(db, package, {s: "replace" for s in package["sections"]})
    assert db.scalar(select(Group).where(Group.name == "Тест-группа")) is None
    assert db.scalar(select(SettingValue).where(SettingValue.key == "appearance.accent")) is None


def test_import_does_not_deactivate_actor(db, world):
    package = transfer.build_package(db, {"users"})
    admin = next(u for u in package["sections"]["users"] if u["username"] == "admin")
    admin["is_active"] = False
    transfer.apply(db, package, {"users": "replace"}, actor="admin")
    assert db.scalar(select(User).where(User.username == "admin")).is_active


# ---------- HTTP ----------

def csrf_of(client, url="/settings/transfer"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(url).text).group(1)


def export_http(client, **extra):
    data = {"sections": ["servers", "groups"], "password": PW, "password2": PW, **extra}
    return client.post("/settings/transfer/export", data=data)


def test_export_http_download(admin, db, world):
    r = export_http(admin)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert ".hlconf" in r.headers["content-disposition"]
    package = transfer.open_package(r.content, PW)
    assert set(package["sections"]) == {"servers", "groups"}
    event = db.scalars(select(SecurityEvent).where(SecurityEvent.kind == "config_export")).one()
    assert PW not in event.detail and "10.0" not in event.detail


def test_export_http_errors(admin):
    r = admin.post("/settings/transfer/export", data={"sections": ["servers"], "password": PW, "password2": "x"})
    assert r.status_code == 400 and "не совпадают" in r.text
    r = admin.post("/settings/transfer/export", data={"password": PW, "password2": PW})
    assert r.status_code == 400 and "хотя бы один раздел" in r.text
    r = admin.post("/settings/transfer/export", data={"sections": ["servers"], "password": "abc", "password2": "abc"})
    assert r.status_code == 400 and "не короче" in r.text


def test_import_http_flow(admin, db, world):
    blob = export_http(admin, passwords="on").content
    wipe_world(db)
    files = {"file": ("config.hlconf", blob)}
    bad = admin.post("/settings/transfer/preview", files=files, data={"password": "wrong-pass"})
    assert bad.status_code == 400 and "неверный пароль" in bad.text

    r = admin.post("/settings/transfer/preview", files={"file": ("config.hlconf", blob)}, data={"password": PW})
    assert r.status_code == 200 and "Предпросмотр" in r.text and "добавится" in r.text
    assert db.scalar(select(Server)) is None  # предпросмотр ничего не меняет
    hidden = re.search(r'name="blob" value="([^"]+)"', r.text).group(1)
    assert base64.b64decode(hidden) == blob and PW not in r.text

    wrong = admin.post("/settings/transfer/apply", data={"blob": hidden, "password": "wrong-pass"})
    assert wrong.status_code == 400 and db.scalar(select(Server)) is None
    done = admin.post("/settings/transfer/apply", data={
        "blob": hidden, "password": PW, "strategy_servers": "skip", "strategy_groups": "replace"})
    assert done.status_code == 200 and "Импорт выполнен" in done.text
    assert db.scalar(select(Server).where(Server.name == "Pi")) is not None
    assert db.scalars(select(SecurityEvent).where(SecurityEvent.kind == "config_import")).one()


def test_import_http_oversize_and_missing_file(admin):
    r = admin.post("/settings/transfer/preview", files={"file": ("big.hlconf", b"x" * (transfer.MAX_FILE + 10))},
                   data={"password": PW})
    assert r.status_code == 400 and "больше" in r.text
    r = admin.post("/settings/transfer/preview", data={"password": PW})
    assert r.status_code == 400 and "Выберите файл" in r.text


def test_permissions(user, viewer, client, db, world):
    for c in (user, viewer):
        assert c.get("/settings/transfer").status_code == 403
        assert export_http(c).status_code == 403
        assert c.post("/settings/transfer/preview", data={"password": PW}).status_code == 403
        assert c.post("/settings/transfer/apply", data={"blob": "", "password": PW}).status_code == 403
        assert "/settings/transfer" not in c.get("/settings/profile").text
    assert client.get("/settings/transfer", follow_redirects=False).status_code in (303, 401, 403)


def test_page_for_admin(admin):
    r = admin.get("/settings/transfer")
    assert r.status_code == 200 and "Экспорт конфигурации" in r.text and r.headers["cache-control"] == "no-store"
    assert "/settings/transfer" in admin.get("/settings/profile").text
