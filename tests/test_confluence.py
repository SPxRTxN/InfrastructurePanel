import pytest
from sqlalchemy import select

from app.models import Board, Issue, Page, PageVersion, Space


@pytest.fixture
def space(admin, db):
    r = admin.post("/confluence/spaces", data={"key": "home", "name": "Дом", "description": "Заметки"},
                   follow_redirects=False)
    assert r.status_code == 303
    return db.scalar(select(Space))


def _page(client, title="Сеть", body="# VLAN\nтекст", **kw):
    r = client.post("/confluence/pages", data={"space": "HOME", "title": title, "body": body, **kw},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


@pytest.fixture
def issue(admin, db):
    admin.post("/jira/boards", data={"key": "HL", "name": "Homelab"})
    board = db.scalar(select(Board))
    admin.post(f"/jira/boards/{board.id}/issues", data={"title": "Поднять VPN", "priority": "medium"})
    return db.scalar(select(Issue))


def test_space_and_tree(admin, space, db):
    root = _page(admin, "Корень")
    child = _page(admin, "Ребёнок", parent_id=str(root))
    _page(admin, "Внук", parent_id=str(child))
    html = admin.get(f"/confluence/pages/{child}").text
    assert "Корень" in html and "Внук" in html and "<h1>VLAN</h1>" in html
    assert admin.get("/confluence/spaces/HOME").status_code == 200
    assert admin.get("/confluence").status_code == 200


def test_space_key_validation(admin, space):
    assert admin.post("/confluence/spaces", data={"key": "HOME", "name": "dup"}).status_code == 400
    assert admin.post("/confluence/spaces", data={"key": "9x", "name": "x"}).status_code == 400


def test_versions_and_restore(admin, space, db):
    pid = _page(admin, body="v1")
    admin.post(f"/confluence/pages/{pid}", data={"title": "Сеть", "body": "v2"})
    admin.post(f"/confluence/pages/{pid}", data={"title": "Сеть", "body": "v2"})  # без изменений — без версии
    page = db.get(Page, pid)
    db.refresh(page)
    assert page.version == 2 and page.body == "v2"
    assert [v.version for v in page.versions] == [2, 1]
    assert "v1" in admin.get(f"/confluence/pages/{pid}/history", params={"v": 1}).text
    admin.post(f"/confluence/pages/{pid}/restore/1")
    db.refresh(page)
    assert page.version == 3 and page.body == "v1"


def test_parent_cycle_rejected(admin, space):
    a = _page(admin, "A")
    b = _page(admin, "B", parent_id=str(a))
    assert admin.post(f"/confluence/pages/{a}", data={"title": "A", "body": "", "parent_id": str(b)}).status_code == 400
    assert admin.post(f"/confluence/pages/{a}", data={"title": "A", "body": "", "parent_id": str(a)}).status_code == 400


def test_delete_reparents_children(admin, space, db):
    a = _page(admin, "A")
    b = _page(admin, "B", parent_id=str(a))
    c = _page(admin, "C", parent_id=str(b))
    admin.post(f"/confluence/pages/{b}/delete")
    db.expire_all()
    assert db.get(Page, c).parent_id == a
    assert db.scalar(select(PageVersion).where(PageVersion.page_id == b)) is None


def test_search_cyrillic(admin, space):
    _page(admin, "Настройка Роутера", body="MikroTik hEX S")
    _page(admin, "Другое", body="про NAS и ДИСКИ")
    html = admin.get("/confluence/search", params={"q": "роутер"}).text
    assert "Настройка Роутера" in html and "Другое" not in html
    assert "Другое" in admin.get("/confluence/search", params={"q": "диски"}).text


def test_artifact_from_issue(admin, space, issue, db):
    html = admin.get(f"/jira/issues/{issue.key}").text
    assert "Создать артефакт" in html and "Редактировать" in html  # can_edit работает в шаблоне
    form = admin.get("/confluence/new", params={"issue": issue.key}).text
    assert "HL-1: Поднять VPN" in form
    pid = _page(admin, "Артефакт", issue=issue.key)
    db.expire_all()
    assert [p.id for p in issue.pages] == [pid]
    # связь видна с обеих сторон
    assert "Поднять VPN" in admin.get(f"/confluence/pages/{pid}").text
    assert "Артефакт" in admin.get(f"/jira/issues/{issue.key}").text


def test_link_unlink_both_sides(admin, space, issue, db):
    pid = _page(admin)
    admin.post(f"/confluence/pages/{pid}/issues", data={"issue": "hl-1"})
    db.expire_all()
    assert issue.pages and issue.pages[0].id == pid
    admin.post(f"/jira/issues/{issue.key}/pages/{pid}/delete")
    db.expire_all()
    assert issue.pages == []
    admin.post(f"/jira/issues/{issue.key}/pages", data={"page_id": str(pid)})
    db.expire_all()
    assert db.get(Page, pid).issues[0].id == issue.id
    admin.post(f"/confluence/pages/{pid}/issues/{issue.id}/delete")
    db.expire_all()
    assert db.get(Page, pid).issues == []
    assert admin.post(f"/confluence/pages/{pid}/issues", data={"issue": "HL-404"}).status_code == 400


def test_viewer_read_only(viewer, admin, space):
    pid = _page(admin)
    html = viewer.get(f"/confluence/pages/{pid}").text
    assert "Редактировать" not in html
    assert viewer.post("/confluence/pages", data={"space": "HOME", "title": "x"}).status_code == 403
    assert viewer.post(f"/confluence/pages/{pid}", data={"title": "x"}).status_code == 403
    assert viewer.get(f"/confluence/pages/{pid}/edit").status_code == 403


def test_page_xss_sanitized(admin, space):
    pid = _page(admin, title="<img src=x onerror=alert(1)>", body="<script>alert(1)</script>\n\n[a](javascript:alert(1))")
    html = admin.get(f"/confluence/pages/{pid}").text
    assert "<script>alert" not in html and "<img src=x" not in html and 'href="javascript' not in html


def test_home_pages_widget(admin, space):
    _page(admin, "Свежая статья")
    html = admin.get("/").text
    assert "Свежая статья" in html and "Статей" in html


def test_jira_board_shows_edit_controls_for_editor(admin, issue):
    assert "Новая задача" in admin.get(f"/jira/boards/{issue.board_id}").text
