"""Включаемые разделы: выключенный раздел пропадает из меню, виджетов и API у всех пользователей."""


def test_disable_jira_and_calendar(admin, user):
    assert 'href="/jira"' in admin.get("/").text
    r = admin.post("/settings/modules", data={"modules.enabled": ["servers", "topology", "confluence"]},
                   follow_redirects=False)
    assert r.status_code == 303
    for client in (admin, user):
        page = client.get("/").text
        assert 'href="/jira"' not in page and 'href="/calendar"' not in page and 'href="/servers"' in page
        assert client.get("/jira").status_code == 403
        assert client.get("/api/calendar/events").status_code in (403, 404)
    # Включаем обратно — всё на месте, данные не трогались
    admin.post("/settings/modules", data={"modules.enabled": ["servers", "topology", "jira", "confluence", "calendar"]})
    assert admin.get("/jira").status_code == 200


def test_modules_section_only_for_settings_manage(user, viewer):
    for client in (user, viewer):
        assert client.get("/settings/modules").status_code == 403
        assert client.post("/settings/modules", data={"modules.enabled": ["servers"]}).status_code == 403
        assert "/settings/modules" not in client.get("/settings/profile").text


def test_infrastructure_menu_and_tabs(admin, viewer, db):
    from app.models import Group, GroupPermission

    page = admin.get("/nodes").text
    assert page.count("Инфраструктура") >= 1 and 'href="/topology"' in page and 'class="tab active"' in page
    assert 'href="/servers" class="nav-item active"' in page  # пункт меню подсвечен на вкладке «Узлы»
    # Только карта сети без серверов — пункт меню ведёт на неё, вкладок нет
    group = db.query(Group).filter_by(name="Наблюдатели").one()
    db.query(GroupPermission).filter(GroupPermission.group_id == group.id,
                                     GroupPermission.perm == "servers.view").delete()
    db.commit()
    page = viewer.get("/topology").text
    assert 'href="/topology" class="nav-item active"' in page and 'class="tabs"' not in page


def test_unknown_module_ignored(admin):
    admin.post("/settings/modules", data={"modules.enabled": ["servers", "evil"]})
    assert admin.get("/topology").status_code == 403
    assert admin.get("/nodes").status_code == 200
