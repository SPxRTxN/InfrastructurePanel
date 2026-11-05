"""Плитки: объединённая «Агент и VPN», сворачивание, сводки, плитка «добавить» с диалогом."""
from app.models import Server


def make_server(db):
    server = Server(name="example", host="192.0.2.10", collector="ssh", ssh_user="user")
    db.add(server)
    db.commit()
    return server


def test_agent_vpn_one_tile(admin, db):
    server = make_server(db)
    html = admin.get(f"/servers/{server.id}/settings").text
    start = html.index('id="agent-vpn"')
    tile = html[start:html.index("</section>", start)]
    assert "VPN</h3>" in tile and "Агент и VPN" not in tile
    assert '<details class="sub-item" id="agent"' in tile and "<summary>Настройки</summary>" in tile
    assert tile.index('id="vpn"') < tile.index('id="agent"') and " open" not in tile.split(">", 1)[0]
    assert 'id="agent"' in tile and f'hx-get="/servers/{server.id}/agent-install"' in tile
    assert 'id="vpn"' in tile and f'hx-get="/servers/{server.id}/vpn"' in tile
    # фрагменты — подразделы, а не отдельные плитки
    assert 'class="card" id="agent"' not in html and 'class="card" id="vpn"' not in html
    assert 'id="agent"' in admin.get(f"/servers/{server.id}/agent-install").text
    assert 'id="vpn"' in admin.get(f"/servers/{server.id}/vpn").text


def test_vpn_loader_needs_settings_manage(user, db):
    server = make_server(db)
    html = user.get(f"/servers/{server.id}/settings").text
    assert 'id="agent-vpn"' in html and f'hx-get="/servers/{server.id}/vpn"' not in html


def test_summaries_and_collapse_script(admin, db):
    server = make_server(db)
    html = admin.get(f"/servers/{server.id}/settings").text
    assert html.count('class="card-summary"') >= 3
    assert "192.0.2.10" in html and "Сервисов: 0 шт." in html
    assert "/static/js/tiles.js" in html
    # узел и сервер: у плиток есть шапка и сводка
    assert 'class="card-summary"' in admin.get(f"/servers/{server.id}").text


def test_add_tile_servers(admin, viewer, db):
    page = admin.get("/servers").text  # даже пустой список — плитка есть
    assert 'class="card server-card add-tile"' in page and "Добавить новый сервер" in page
    dlg = page[page.index('<dialog id="new-server-dialog"'):]
    assert 'action="/servers"' in dlg[:dlg.index("</dialog>")]
    assert 'data-dialog-open="new-server-dialog"' in page
    seen = viewer.get("/servers").text
    assert "add-tile" not in seen and "<dialog" not in seen


def test_add_tile_nodes(admin, viewer, db):
    page = admin.get("/nodes").text
    assert "Добавить новый узел" in page
    dlg = page[page.index('<dialog id="new-node-dialog"'):]
    assert 'action="/nodes"' in dlg[:dlg.index("</dialog>")]
    seen = viewer.get("/nodes").text
    assert "add-tile" not in seen and "<dialog" not in seen
