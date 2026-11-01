from datetime import date

import pytest
from sqlalchemy import select

from app.models import Board, BoardColumn, Issue, User
from app.services.markdown import render_markdown


@pytest.fixture
def board(admin, db):
    r = admin.post("/jira/boards", data={"key": "hl", "name": "Homelab"}, follow_redirects=False)
    assert r.status_code == 303
    return db.scalar(select(Board))


def _issue(client, board, **kw):
    data = {"title": "Настроить VLAN", "priority": "high", "labels": "Сеть, mikrotik, сеть", "due_date": "2026-11-05",
            "description": "**важно**", **kw}
    return client.post(f"/jira/boards/{board.id}/issues", data=data, follow_redirects=False)


def test_board_default_columns(board):
    assert board.key == "HL"
    assert [c.name for c in board.columns] == ["To Do", "In Progress", "Review", "Done"]


def test_board_key_validation(admin):
    assert admin.post("/jira/boards", data={"key": "1x", "name": "x"}).status_code == 400
    assert admin.post("/jira/boards", data={"key": "AB", "name": ""}).status_code == 400


def test_issue_keys_increment(admin, board, db):
    _issue(admin, board)
    _issue(admin, board, title="Вторая")
    keys = [i.key for i in db.scalars(select(Issue).order_by(Issue.number))]
    assert keys == ["HL-1", "HL-2"]
    i = db.scalar(select(Issue))
    assert i.labels == "сеть,mikrotik" and i.due_date == date(2026, 11, 5) and i.priority == "high"
    page = admin.get("/jira/issues/HL-1")
    assert page.status_code == 200 and "<strong>важно</strong>" in page.text
    assert admin.get("/jira/issues/hl-1").status_code == 200
    assert admin.get("/jira/issues/HL-99").status_code == 404


def test_issue_validation(admin, board):
    assert _issue(admin, board, title="").status_code == 400
    assert _issue(admin, board, priority="urgent").status_code == 400
    assert _issue(admin, board, due_date="31.12.2026").status_code == 400


def test_move_issue(admin, board, db):
    _issue(admin, board)
    _issue(admin, board, title="B")
    a, b = db.scalars(select(Issue).order_by(Issue.number)).all()
    done = board.columns[-1]
    r = admin.post(f"/api/jira/issues/{a.id}/move", json={"column_id": done.id, "order": [b.id, a.id]})
    assert r.status_code == 200 and r.json()["column"] == "Done"
    db.expire_all()
    assert a.column_id == done.id and a.is_done
    assert b.position == 0 and a.position == 1


def test_move_to_foreign_board_rejected(admin, board, db):
    _issue(admin, board)
    admin.post("/jira/boards", data={"key": "XX", "name": "Other"})
    other_col = db.scalar(select(BoardColumn).join(Board).where(Board.key == "XX"))
    issue = db.scalar(select(Issue))
    assert admin.post(f"/api/jira/issues/{issue.id}/move", json={"column_id": other_col.id, "order": []}).status_code == 400


def test_move_requires_csrf_and_edit(admin, viewer, board, db):
    _issue(admin, board)
    issue = db.scalar(select(Issue))
    col = board.columns[1].id
    assert viewer.post(f"/api/jira/issues/{issue.id}/move", json={"column_id": col, "order": []}).status_code == 403
    admin.headers.pop("X-CSRF-Token")
    assert admin.post(f"/api/jira/issues/{issue.id}/move", json={"column_id": col, "order": []}).status_code == 403


def test_search_and_filters(admin, board, db):
    uid = db.scalar(select(User).where(User.username == "user")).id
    _issue(admin, board, title="Обновить прошивку Роутера", labels="сеть", assignee_id=str(uid))
    _issue(admin, board, title="Купить диск", labels="nas", priority="low")
    url = f"/jira/boards/{board.id}"
    assert "Купить диск" not in admin.get(url, params={"q": "роутер"}).text  # регистр кириллицы
    assert "Обновить прошивку" in admin.get(url, params={"q": "РОУТЕР"}).text
    assert "Обновить прошивку" not in admin.get(url, params={"label": "nas"}).text
    assert "Купить диск" not in admin.get(url, params={"assignee": str(uid)}).text
    assert "Обновить прошивку" not in admin.get(url, params={"priority": "low"}).text


def test_update_and_delete_issue(admin, board, db):
    _issue(admin, board)
    review = board.columns[2]
    r = admin.post("/jira/issues/HL-1", data={"title": "Новое", "priority": "low", "column_id": str(review.id)},
                   follow_redirects=False)
    assert r.status_code == 303
    issue = db.scalar(select(Issue))
    db.refresh(issue)
    assert issue.title == "Новое" and issue.column_id == review.id and issue.due_date is None
    admin.post("/jira/issues/HL-1/delete")
    assert db.scalar(select(Issue)) is None


def test_columns_manage(admin, board, db):
    admin.post(f"/jira/boards/{board.id}/columns", data={"name": "Blocked"})
    db.refresh(board)
    assert [c.name for c in board.columns] == ["To Do", "In Progress", "Review", "Blocked", "Done"]
    blocked = board.columns[3]
    admin.post(f"/jira/columns/{blocked.id}", data={"name": "Заблокировано", "move": "left"})
    db.expire_all()
    assert [c.name for c in board.columns][2] == "Заблокировано"
    _issue(admin, board, column_id=str(blocked.id))
    assert admin.post(f"/jira/columns/{blocked.id}/delete").status_code == 400


def test_viewer_cannot_edit(viewer, admin, board):
    assert viewer.get(f"/jira/boards/{board.id}").status_code == 200
    assert _issue(viewer, board).status_code == 403
    assert viewer.post("/jira/boards", data={"key": "VV", "name": "v"}).status_code == 403
    assert "Новая задача" not in viewer.get(f"/jira/boards/{board.id}").text


def test_home_tasks_widget(admin, board):
    _issue(admin, board)
    html = admin.get("/").text
    assert "Открытых задач" in html and "data-doughnut" in html and "HL-1" in html


@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>",
    "[x](javascript:alert(1))",
    '<img src=x onerror="alert(1)">',
    "<a href='javascript:alert(1)'>x</a>",
])
def test_markdown_sanitized(payload):
    # Опасное должно стать текстом: ни реальных тегов, ни ссылок с javascript:
    html = render_markdown(payload)
    assert "<script" not in html and "<img" not in html and 'href="javascript' not in html
    assert "<a href='javascript" not in html


def test_markdown_features():
    html = render_markdown("# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n~~x~~ [l](https://e.x)")
    assert "<h1>" in html and "<table>" in html and "<s>x</s>" in html
    assert 'rel="noopener noreferrer nofollow"' in html


def test_markdown_preview_endpoint(admin):
    r = admin.post("/markdown/preview", data={"description": "**b** <script>x</script>"})
    assert "<strong>b</strong>" in r.text and "<script>" not in r.text


def test_xss_in_issue_page(admin, board):
    _issue(admin, board, title="<script>alert(1)</script>", description="<img src=x onerror=alert(1)>")
    html = admin.get("/jira/issues/HL-1").text
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
