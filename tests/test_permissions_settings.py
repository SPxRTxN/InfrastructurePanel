from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select

from app.db import Base, engine
from app.models import Group, User
from app.services.settings_registry import get_setting


def test_migrations_match_models():
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_default_groups(db):
    groups = {g.name: {p.perm for p in g.permissions} for g in db.scalars(select(Group))}
    assert "users.manage" in groups["Администраторы"]
    assert "settings.manage" not in groups["Пользователи"] and "jira.edit" in groups["Пользователи"]
    assert all(p.endswith(".view") for p in groups["Наблюдатели"])


def test_menu_shows_only_permitted(viewer, db):
    group = db.scalar(select(Group).where(Group.name == "Наблюдатели"))
    group.permissions = [p for p in group.permissions if p.perm != "jira.view"]
    db.commit()
    html = viewer.get("/").text
    assert 'href="/servers"' in html
    assert 'href="/jira"' not in html
    assert 'href="/settings"' in html


def test_users_page_requires_users_manage(admin, user):
    assert admin.get("/settings/users").status_code == 200
    assert user.get("/settings/users").status_code == 403
    assert user.post("/settings/users", data={"username": "x", "password": "12345678"}).status_code == 403


def test_admin_creates_user(admin, db):
    gid = db.scalar(select(Group).where(Group.name == "Наблюдатели")).id
    r = admin.post("/settings/users", data={"username": "new", "password": "long-password", "groups": [gid]},
                   follow_redirects=False)
    assert r.status_code == 303
    new = db.scalar(select(User).where(User.username == "new"))
    assert new and [g.name for g in new.groups] == ["Наблюдатели"]
    assert "long-password" not in admin.get("/settings/users").text


def test_cannot_remove_last_admin(admin, db):
    me = db.scalar(select(User).where(User.username == "admin"))
    r = admin.post(f"/settings/users/{me.id}", data={"display_name": "", "is_active": "on"})
    assert r.status_code == 400
    db.expire_all()
    assert me.can("users.manage")


def test_group_permissions_update(admin, db):
    g = db.scalar(select(Group).where(Group.name == "Пользователи"))
    admin.post(f"/settings/groups/{g.id}", data={"perms": ["jira.view", "bogus.perm"]})
    db.expire_all()
    assert {p.perm for p in g.permissions} == {"jira.view"}


def test_builtin_group_not_deleted(admin, db):
    g = db.scalar(select(Group).where(Group.name == "Наблюдатели"))
    assert admin.post(f"/settings/groups/{g.id}/delete").status_code == 400


def test_user_scope_settings(user, admin, db):
    r = user.post("/settings/appearance", data={"appearance.theme": "light", "appearance.accent": "green",
                                                "appearance.density": "compact"}, follow_redirects=False)
    assert r.status_code == 303
    uid = db.scalar(select(User).where(User.username == "user")).id
    assert get_setting(db, "appearance.theme", uid) == "light"
    html = user.get("/").text
    assert 'data-theme="light"' in html and 'data-accent="green"' in html
    # у администратора своя тема по умолчанию
    assert 'data-theme="dark"' in admin.get("/").text


def test_invalid_setting_rejected(user):
    r = user.post("/settings/appearance", data={"appearance.theme": "neon"})
    assert r.status_code == 400


def test_global_setting_needs_settings_manage(user, admin, db):
    assert user.post("/settings/intervals", data={"servers.check_interval": "30"}).status_code == 403
    assert admin.post("/settings/intervals", data={"servers.check_interval": "30"},
                      follow_redirects=False).status_code == 303
    assert get_setting(db, "servers.check_interval") == 30


def test_home_widgets_order(user, db):
    user.post("/settings/home", data={"home.widgets": ["pages", "servers", "nope"]})
    uid = db.scalar(select(User).where(User.username == "user")).id
    assert get_setting(db, "home.widgets", uid) == ["pages", "servers"]


def test_sidebar_toggle(user, db):
    assert user.post("/settings/sidebar").status_code == 204
    assert 'class="sidebar-collapsed"' in user.get("/").text


def test_pages_render(admin):
    for url in ["/", "/settings/profile", "/settings/users", "/settings/groups", "/settings/appearance",
                "/settings/home", "/settings/calendar", "/settings/intervals", "/alerts", "/vpn", "/archive"]:
        assert admin.get(url).status_code == 200, url
