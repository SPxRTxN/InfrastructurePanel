"""«Сервер» — сводка, «Узел» — настройка: страница сервера без форм правки, общий фрагмент настроек,
меню без Оповещений/VPN/Архива, раздел настроек «Интервалы и хранение»."""
import pytest
from sqlalchemy import select

from app.models import Node, Server, ServerCredential, SettingValue
from app.services import agents


@pytest.fixture
def server(db):
    s = Server(name="Mini", host="192.168.1.39", collector="ssh", ssh_user="root")
    db.add(s)
    db.commit()
    return s


@pytest.fixture
def node(db, server):
    n = Node(name="mini", server=server)
    agents.issue_token(n)
    db.add(n)
    db.commit()
    return n


def test_server_page_is_info_only(admin, server):
    page = admin.get(f"/servers/{server.id}").text
    assert f'href="/servers/{server.id}/console"' in page and f'href="/servers/{server.id}/history.json"' in page
    assert f'href="/servers/{server.id}/settings"' in page  # «Настройки узла»
    for gone in ('action="/servers/', 'action="/services/', "Удалить везде", "Проверить сейчас",
                 "Доступ: входы и ключи", "<dialog", 'name="ssh_key_path"'):
        assert gone not in page, gone


def test_server_page_shows_node_charts_and_links_to_node_settings(admin, node):
    page = admin.get(f"/servers/{node.server.id}").text
    assert f'data-history-url="/api/nodes/{node.id}/history"' in page
    assert f'href="/nodes/{node.id}#settings"' in page
    # На странице узла истории нет — только последние значения
    assert "node-charts" not in admin.get(f"/nodes/{node.id}").text


def test_server_settings_page_without_node(admin, server):
    page = admin.get(f"/servers/{server.id}/settings").text
    for part in ("Устройство", "Подключение к ОС", "Подключение к BMC", "Сервисы", "Проверить сейчас",
                 "Удалить везде", 'name="kind"', "<dialog"):
        assert part in page, part
    # Окно удаления: два варианта (для сервера без агента — без выбора) и свои кнопки, без confirm
    assert "Оставить агента на узле" not in page and "data-dialog-close" in page and "data-confirm=\"Удалить везде" not in page


def test_server_settings_redirects_to_node_when_agent(admin, node):
    r = admin.get(f"/servers/{node.server.id}/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/nodes/{node.id}#settings"


def test_node_page_shows_settings_with_agent_choice(admin, node):
    page = admin.get(f"/nodes/{node.id}").text
    assert 'id="settings"' in page and "Оставить агента на узле" in page and 'name="remove_agent" value="0"' in page
    assert f'action="/nodes/{node.id}/delete"' in page


def test_blocks_by_kind(admin, db, server):
    assert "Подключение к BMC" in admin.get(f"/servers/{server.id}/settings").text
    admin.post(f"/servers/{server.id}", data={"name": "ПК", "host": "h", "collector": "ssh", "kind": "pc",
                                              "enabled": "on"})
    db.refresh(server)
    assert server.kind == "pc"
    page = admin.get(f"/servers/{server.id}/settings").text
    assert "Подключение к ОС" in page and "Подключение к BMC" not in page
    assert admin.post(f"/servers/{server.id}", data={"name": "x", "host": "h", "collector": "ssh",
                                                     "kind": "mainframe"}).status_code == 400


def test_creds_split_by_kind_and_back_redirect(admin, db, server):
    r = admin.post(f"/servers/{server.id}/credentials", data={"kind": "bmc", "label": "iDRAC", "host": "10.0.0.5",
                                                              "username": "root", "password": "p-secret"},
                   follow_redirects=False)
    assert r.headers["location"] == f"/servers/{server.id}/settings#access"
    admin.post(f"/servers/{server.id}/credentials", data={"kind": "os", "label": "adm", "username": "adm"})
    page = admin.get(f"/servers/{server.id}/settings").text
    bmc = page.split('id="bmc"')[1].split("</section>")[0]
    os_ = page.split('id="access"')[1].split("</section>")[0]
    assert "iDRAC" in bmc and "iDRAC" not in os_ and "adm" in os_
    assert "p-secret" not in page and db.scalar(select(ServerCredential)).password_enc != "p-secret"


def test_settings_permissions(viewer, user, admin, db, server):
    assert viewer.get(f"/servers/{server.id}/settings").status_code == 403
    assert viewer.get(f"/servers/{server.id}").status_code == 200
    assert user.get(f"/servers/{server.id}/settings").status_code == 200
    # Блок доступа — только с servers.console (у «Пользователей» его нет)
    assert "Подключение к ОС" not in user.get(f"/servers/{server.id}/settings").text
    assert "Подключение к ОС" in admin.get(f"/servers/{server.id}/settings").text
    assert viewer.post(f"/servers/{server.id}/delete").status_code == 403


def test_create_server_card_for_node(admin, user, viewer, db):
    n = Node(name="pi")
    agents.issue_token(n)
    n.ips_json = '["192.168.1.10"]'
    db.add(n)
    db.commit()
    assert viewer.post(f"/nodes/{n.id}/server").status_code == 403
    assert admin.post(f"/nodes/{n.id}/server", follow_redirects=False).status_code == 303
    db.refresh(n)
    assert n.server.name == "pi" and n.server.host == "192.168.1.10" and n.server.collector == "agent"
    assert admin.post(f"/nodes/{n.id}/server").status_code == 400  # уже есть
    n2 = Node(name="bare")
    agents.issue_token(n2)
    db.add(n2)
    db.commit()
    assert admin.post(f"/nodes/{n2.id}/server").status_code == 400  # адреса нет
    assert admin.post(f"/nodes/{n2.id}/server", data={"host": "10.1.1.1"}).status_code == 200


def test_delete_dialog_keeps_remove_agent_field(admin, db, node):
    r = admin.post(f"/servers/{node.server.id}/delete", data={"remove_agent": "0"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/servers"
    assert db.scalar(select(Server)) is None


def test_menu_and_settings_nav(admin, viewer):
    menu = admin.get("/servers").text.split('class="tabs"')[1].split("</nav>")[0]
    for gone in ("/alerts", "/vpn", "/archive"):
        assert f'href="{gone}"' not in menu
    for url in ("/alerts", "/vpn", "/archive"):
        page = admin.get(url)
        assert page.status_code == 200
        nav = page.text.split('class="card subnav"')[1].split("</nav>")[0]
        assert 'href="/alerts"' in nav and 'href="/vpn"' in nav and 'href="/archive"' in nav
        assert 'href="/settings/profile"' in nav
    # Прежние права: VPN — только settings.manage; в навигации виден раздел по праву
    assert viewer.get("/vpn").status_code == 403
    nav = viewer.get("/alerts").text.split('class="card subnav"')[1].split("</nav>")[0]
    assert 'href="/vpn"' not in nav and 'href="/archive"' in nav
    assert 'href="/settings/servers"' not in admin.get("/settings/profile").text
    assert admin.get("/settings/servers").status_code == 404


def test_intervals_section(admin, user, db):
    page = admin.get("/settings/intervals").text
    for key in ("servers.check_interval", "agents.interval", "agents.history_every", "history.retention_days"):
        assert f'name="{key}"' in page
    r = admin.post("/settings/intervals", data={"servers.check_interval": "30", "agents.interval": "5",
                                                 "agents.history_every": "120", "history.retention_days": "7"},
                   follow_redirects=False)
    assert r.status_code == 303
    row = db.scalar(select(SettingValue).where(SettingValue.key == "history.retention_days"))
    assert row.value == "7" or row.value == 7
    assert admin.post("/settings/intervals", data={"servers.check_interval": "1"}).status_code == 400
    assert user.get("/settings/intervals").status_code == 403
    assert user.post("/settings/intervals", data={"servers.check_interval": "30"}).status_code == 403
