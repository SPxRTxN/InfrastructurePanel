"""Вёрстка форм: подписи с вложенными inline-элементами и ключевые страницы."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted((ROOT / "app/templates").rglob("*.html"))
CSS = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")

PAGES = ["/", "/servers", "/jira", "/confluence", "/calendar", "/topology", "/alerts",
         "/vpn", "/archive", "/nodes", "/settings/profile", "/settings/users",
         "/settings/groups", "/settings/security", "/settings/servers", "/settings/appearance"]


def _css_rule(selector_start: str) -> str:
    m = re.search(rf"^{re.escape(selector_start)}[^{{]*\{{([^}}]*)\}}", CSS, re.M)
    assert m, f"в app.css нет правила {selector_start}"
    return m.group(1)


def test_label_is_not_flex():
    # label — обычный блок, иначе inline-элементы подписи рвутся на отдельные строки
    rule = _css_rule("label, .field")
    assert "display: block" in rule
    assert "flex" not in rule


def test_long_values_wrap():
    assert re.search(r"^code[^{]*\{[^}]*overflow-wrap: anywhere", CSS, re.M)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: str(p.relative_to(ROOT / "app/templates")))
def test_label_text_not_next_to_flex_breakers(path):
    # Защита от возврата к flex-подписям: label не должен задаваться display:flex в шаблонах
    text = path.read_text(encoding="utf-8")
    for m in re.finditer(r"<label\b[^>]*style=\"[^\"]*flex[^\"]*\"", text):
        pytest.fail(f"{path.name}: label с inline flex: {m.group(0)}")


def test_tables_in_table_wrap():
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"<table\b", text):
            before = text[max(0, m.start() - 80):m.start()]
            assert 'class="table-wrap"' in before, f"{path.name}: таблица вне .table-wrap"


@pytest.mark.parametrize("url", PAGES)
def test_key_pages_render(admin, url):
    r = admin.get(url)
    assert r.status_code == 200, url
    assert "<html" in r.text
