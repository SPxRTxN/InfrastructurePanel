"""Режим обновления панели: обслуживание (503), запрос службе, итоги done/failed/зависший requested, права,
хост-скрипты (только статические проверки, без запуска)."""
import json
import re
import subprocess
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import BASE_DIR
from app.models import Maintenance, Notification, SecurityEvent, User
from app.services import maintenance
from app.utils import utcnow


@pytest.fixture
def upd(tmp_path, monkeypatch):
    d = tmp_path / "update"
    monkeypatch.setattr(maintenance, "update_dir", lambda: d)
    return d


def _on(db, version="1.2.3", since_ago=0):
    now = utcnow() - timedelta(minutes=since_ago)
    db.add(Maintenance(id=1, enabled=True, since=now, eta=now + timedelta(minutes=5), by="admin",
                       message=f"Технические работы: обновление панели до {version}", target_version=version))
    db.commit()


def _status(d, state, step="", ago=0, started_ago=0, error=""):
    d.mkdir(exist_ok=True)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    maintenance._write_json(d / "status.json", {
        "state": state, "step": step, "version": "1.2.3", "error": error,
        "ts": (utcnow() - timedelta(minutes=ago)).strftime(fmt),
        "started": (utcnow() - timedelta(minutes=started_ago)).strftime(fmt)})


# ---------- режим обслуживания ----------

def test_maintenance_blocks_user_and_anonymous(client, user, db, upd):
    _on(db)
    r = client.get("/")
    assert r.status_code == 503 and "Технические работы" in r.text
    assert int(r.headers["Retry-After"]) >= 30
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]  # заголовки и на странице 503
    assert user.get("/jira").status_code == 503
    j = user.get("/jira", headers={"Accept": "application/json"})
    assert j.status_code == 503 and j.json()["maintenance"] is True
    assert user.get("/", headers={"HX-Request": "true"}).status_code == 503


def test_maintenance_open_paths(client, db, upd):
    _on(db)
    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    with client.websocket_connect("/agent/ws") as ws:  # агент доходит до своей проверки токена, а не до 503
        ws.send_text("{}")
        assert ws.receive_json()["type"] == "error"


def test_maintenance_admin_sees_pages(admin, db, upd):
    _on(db)
    assert admin.get("/").status_code == 200
    assert admin.get("/settings/update").status_code == 200


def test_no_maintenance_means_normal_work(user, db):
    assert user.get("/").status_code == 200


# ---------- запрос на обновление ----------

def test_start_writes_request_and_enables_mode(admin, db, upd):
    r = admin.post("/settings/update/start", data={"version": "v1.2.3"}, follow_redirects=False)
    assert r.status_code == 303
    req = json.loads((upd / "request.json").read_text())
    assert set(req) == {"version", "requested_by", "requested_at"}
    assert req["version"] == "v1.2.3" and req["requested_by"] == "admin"
    assert json.loads((upd / "status.json").read_text())["state"] == "requested"
    row = db.get(Maintenance, 1)
    assert row.enabled and row.target_version == "v1.2.3" and row.eta > row.since and row.by == "admin"
    assert db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "panel_update"))
    page = admin.get("/settings/update").text
    assert "Идёт обновление до v1.2.3" in page and 'id="update-cancel"' in page
    assert admin.post("/settings/update/start", data={"version": "latest"}).status_code == 400  # уже идёт


@pytest.mark.parametrize("bad", ["", "1.2", "1.2.3; rm -rf /", "$(id)", "v1.2.3\n", "latest ", "../1.2.3", "1.2.3-rc1"])
def test_bad_version_is_400_and_writes_nothing(admin, db, upd, bad):
    assert admin.post("/settings/update/start", data={"version": bad}).status_code == 400
    assert not upd.exists() or not list(upd.iterdir())
    assert db.get(Maintenance, 1) is None


def test_latest_is_accepted(admin, upd):
    assert admin.post("/settings/update/start", data={"version": "latest"}, follow_redirects=False).status_code == 303


def test_only_settings_manage(user, viewer, client, db, upd):
    for c in (user, viewer):
        assert c.get("/settings/update").status_code == 403
        assert c.get("/settings/update/status").status_code == 403
        assert c.post("/settings/update/start", data={"version": "1.0.0"}).status_code == 403
        assert c.post("/settings/update/cancel").status_code == 403
    assert not upd.exists() and db.get(Maintenance, 1) is None
    assert client.get("/settings/update", follow_redirects=False).status_code == 303


def test_cancel_only_while_requested(admin, db, upd):
    admin.post("/settings/update/start", data={"version": "1.2.3"})
    r = admin.post("/settings/update/cancel", follow_redirects=False)
    assert r.status_code == 303
    assert not (upd / "request.json").exists() and not (upd / "status.json").exists()
    db.expire_all()
    assert not db.get(Maintenance, 1).enabled
    # служба уже взяла запрос: request.json нет, статус running
    admin.post("/settings/update/start", data={"version": "1.2.3"})
    (upd / "request.json").unlink()
    _status(upd, "running", "pull")
    assert admin.post("/settings/update/cancel").status_code == 400
    db.expire_all()
    assert db.get(Maintenance, 1).enabled


def test_status_endpoint_shows_log(admin, db, upd):
    _on(db)
    _status(upd, "running", "pull")
    (upd / "log.txt").write_text("== Загрузка <img src=x>\n")
    s = admin.get("/settings/update/status").json()
    assert s["active"] and s["status"]["step"] == "pull" and not s["can_cancel"]
    assert "Загрузка" in s["log"]
    assert "<img" not in admin.get("/settings/update").text  # лог на странице экранирован


def test_version_and_eta(admin, upd, monkeypatch):
    from app import __version__

    assert __version__ in admin.get("/settings/update").text  # версия из app/__init__.py (релизы)
    assert maintenance.eta_seconds() == 180
    upd.mkdir()
    (upd / "history.jsonl").write_text(
        '{"version":"1.0.0","seconds":300,"state":"done"}\n{"version":"1.0.1","seconds":100,"state":"failed"}\n'
        '{"version":"1.0.2","seconds":200,"state":"done"}\n')
    assert maintenance.eta_seconds() == 250


# ---------- итог обновления ----------

def test_done_disables_mode_and_notifies(db, upd):
    _on(db)
    _status(upd, "done", "finish")
    maintenance.check(db)
    db.expire_all()
    assert not db.get(Maintenance, 1).enabled
    admin_id = db.scalar(select(User.id).where(User.username == "admin"))
    assert db.scalar(select(Notification).where(Notification.user_id == admin_id,
                                                Notification.title == "Панель обновлена"))
    ev = db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "panel_update"))
    assert ev.ok
    viewer_id = db.scalar(select(User.id).where(User.username == "viewer"))
    assert not db.scalar(select(Notification).where(Notification.user_id == viewer_id))


def test_failed_disables_mode_with_reason(db, upd):
    _on(db)
    _status(upd, "failed", "healthy", error="код 1")
    maintenance.check(db)
    db.expire_all()
    assert not db.get(Maintenance, 1).enabled
    ev = db.scalar(select(SecurityEvent).where(SecurityEvent.kind == "panel_update"))
    assert not ev.ok and "ожидание готовности" in ev.detail and "код 1" in ev.detail
    assert db.scalar(select(Notification).where(Notification.title == "Обновление панели не удалось"))


def test_stuck_requested_times_out(db, upd):
    _on(db, since_ago=11)
    _status(upd, "requested", "requested", ago=11)
    (upd / "request.json").write_text("{}")
    maintenance.check(db)
    db.expire_all()
    assert not db.get(Maintenance, 1).enabled
    assert not (upd / "request.json").exists()
    st = json.loads((upd / "status.json").read_text())
    assert st["state"] == "failed" and "служба обновления не запущена" in st["error"]
    assert "служба обновления не запущена" in db.scalar(select(SecurityEvent.detail))


def test_fresh_requested_and_running_keep_mode(db, upd):
    _on(db)
    _status(upd, "requested", "requested", ago=2)
    maintenance.check(db)
    _status(upd, "running", "pull", started_ago=5)
    maintenance.check(db)
    db.expire_all()
    assert db.get(Maintenance, 1).enabled


def test_missing_status_waits_then_times_out(db, upd):
    _on(db)
    maintenance.check(db)
    db.expire_all()
    assert db.get(Maintenance, 1).enabled
    db.get(Maintenance, 1).since = utcnow() - timedelta(minutes=12)
    db.commit()
    maintenance.check(db)
    db.expire_all()
    assert not db.get(Maintenance, 1).enabled


def test_check_noop_when_off(db, upd):
    _status(upd, "failed", "pull")
    maintenance.check(db)
    assert not db.scalar(select(SecurityEvent))


# ---------- хост-скрипты и юниты (без запуска) ----------

def _read(name):
    return (BASE_DIR / "tools" / name).read_text()


@pytest.mark.parametrize("script", ["deploy.sh", "update-from-panel.sh", "install-updater.sh"])
def test_scripts_syntax(script):
    assert subprocess.run(["bash", "-n", str(BASE_DIR / "tools" / script)], capture_output=True).returncode == 0


def test_update_from_panel_reads_only_version():
    s = _read("update-from-panel.sh")
    assert '"version"' in s and r"^(v?[0-9]+\.[0-9]+\.[0-9]+|latest)$" in s
    assert "eval" not in s and "source " not in s and "jq" not in s
    assert 'deploy.sh" pull "$VERSION"' in s and "log.txt" in s and "status.json" in s
    for state in ("running", "done", "failed"):
        assert state in s
    assert s.index("[[ \"$VERSION\" =~") < s.index('deploy.sh" pull')  # проверка до вызова


def test_deploy_pull_mode_and_build_unchanged():
    s = _read("deploy.sh")
    assert 'if [ "$TARGET" = pull ]' in s and "UPDATE_STATUS_FILE" in s
    for step in ("backup", "pull", "up", "healthy", "rollback"):
        assert re.search(rf"^\s*step {step}$", s, re.M), step
    # прежнее поведение режимов
    assert 'if [ "$TARGET" = build ]; then' in s and "docker build --network host" in s
    assert "docker compose pull dashboard" in s and "export DASHBOARD_TAG=rollback" in s


def test_systemd_units_and_installer():
    path, svc, inst = _read("homelab-updater.path"), _read("homelab-updater.service"), _read("install-updater.sh")
    assert "PathChanged=@DATA_DIR@/update/request.json" in path and "Unit=homelab-updater.service" in path
    assert "ExecStart=@PROJECT_DIR@/tools/update-from-panel.sh @DATA_DIR@" in svc and "Type=oneshot" in svc
    assert "systemctl enable --now homelab-updater.path" in inst and "DATA_DIR" in inst
    assert "@DATA_DIR@" in inst and "id -u" in inst


def test_migration_chain():
    m = (BASE_DIR / "migrations" / "versions" / "0017_maintenance.py").read_text()
    assert "revision = '0017_maintenance'" in m and "down_revision = '0016_merge'" in m
    assert "drop_table" in m and m.count("create_table") == 1
