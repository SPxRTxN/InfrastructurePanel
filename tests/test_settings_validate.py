"""Меню настроек: каждый пункт открывается, устаревшие записи настроек находятся и убираются, значения — по реестру."""
import json

from app.db import SessionLocal
from app.models import SettingValue
from app.routers import settings as settings_router
from app.services import settings_registry as reg


def test_every_settings_menu_entry_opens(admin):
    for key, _label, _perm in settings_router.NAV:
        url = settings_router.EXTERNAL.get(key, f"/settings/{key}")
        r = admin.get(url)
        assert r.status_code == 200, (key, r.status_code)


def test_every_section_has_page():
    nav = {k for k, _l, _p in settings_router.NAV}
    for s in reg.SETTINGS:
        assert s.section in nav, s.key  # настройка без пункта меню — потерянная


def _put(key, value, user_id=None):
    with SessionLocal() as db:
        db.add(SettingValue(key=key, user_id=user_id, value=value))
        db.commit()


def test_stale_values_fall_back_to_default():
    _put("appearance.accent", json.dumps("removed-colour"))
    _put("home.widgets", json.dumps(["kpi", "gone", "pages"]))
    _put("servers.check_interval", "{bad json")
    with SessionLocal() as db:
        assert reg.get_setting(db, "appearance.accent") == reg.REGISTRY["appearance.accent"].default
        assert reg.get_setting(db, "home.widgets") == ["kpi", "pages"]
        assert reg.get_setting(db, "servers.check_interval") == reg.REGISTRY["servers.check_interval"].default


def test_prune_removes_unknown_and_fixes_lists():
    _put("old.removed_setting", json.dumps("x"))
    _put("home.widgets", json.dumps(["kpi", "gone"]))
    _put("appearance.theme", json.dumps("light"))  # годная — остаётся
    with SessionLocal() as db:
        reasons = {r.key: why for r, why in reg.stale_rows(db)}
        assert set(reasons) == {"old.removed_setting", "home.widgets"}
        assert reg.prune_stale(db) == 2
        assert reg.stale_rows(db) == []
        keys = {r.key: json.loads(r.value) for r in db.query(SettingValue).all()}
    assert "old.removed_setting" not in keys and keys["home.widgets"] == ["kpi"] and keys["appearance.theme"] == "light"
