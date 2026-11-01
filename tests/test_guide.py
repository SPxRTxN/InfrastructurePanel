from sqlalchemy import select

from app import cli
from app.models import Page, PageVersion, Space
from app.services import guide
from tests.conftest import login


def _pages(db):
    space = db.scalar(select(Space).where(Space.key == guide.SPACE_KEY))
    return space, list(db.scalars(select(Page).where(Page.space_id == space.id).order_by(Page.id)))


def test_first_sync_creates_space_and_tree(db):
    stats = guide.sync(db)
    space, pages = _pages(db)
    n = len(guide._sources())
    assert n >= 15 and stats == {"created": n, "updated": 0, "unchanged": 0}
    assert space.name == "Руководство"
    root = next(p for p in pages if p.parent_id is None)
    assert root.title == "Руководство по панели"
    assert all(p.parent_id == root.id for p in pages if p is not root)
    assert {"Начало работы", "Jira", "VPN", "Частые вопросы"} <= {p.title for p in pages}


def test_second_sync_changes_nothing(db):
    guide.sync(db)
    versions = db.scalar(select(PageVersion.id).limit(1))
    stats = guide.sync(db)
    assert stats["created"] == stats["updated"] == 0 and stats["unchanged"] == len(guide._sources())
    _, pages = _pages(db)
    assert all(p.version == 1 for p in pages) and versions


def test_changed_source_makes_new_version(db, monkeypatch, tmp_path):
    (tmp_path / "00-Руководство.md").write_text("# Корень\n\nтекст\n", encoding="utf-8")
    (tmp_path / "01-Тест.md").write_text("# Тест\n\nстарое\n", encoding="utf-8")
    monkeypatch.setattr(guide, "GUIDE_DIR", tmp_path)
    guide.sync(db)
    (tmp_path / "01-Тест.md").write_text("# Тест\n\nновое\n", encoding="utf-8")
    assert guide.sync(db) == {"created": 0, "updated": 1, "unchanged": 1}
    _, pages = _pages(db)
    page = next(p for p in pages if p.title == "Тест")
    assert page.version == 2 and "новое" in page.body
    assert [v.version for v in page.versions] == [2, 1] and "старое" in page.versions[1].body


def test_guide_page_renders_sanitized(db, client):
    guide.sync(db)
    _, pages = _pages(db)
    page = pages[1]
    page.body += "\n\n<script>alert(1)</script>\n"
    db.commit()
    login(client, "viewer")
    r = client.get(f"/confluence/pages/{page.id}")
    assert r.status_code == 200 and "<script>alert(1)</script>" not in r.text
    assert "homelab-guide" not in r.text  # метка не видна читателю


def test_cli_guide_sync(db, capsys):
    assert cli.main(["guide-sync"]) == 0
    assert "Руководство: создано" in capsys.readouterr().out
    assert cli.main(["guide-sync"]) == 0
    assert "создано 0, обновлено 0" in capsys.readouterr().out
