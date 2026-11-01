"""Каждая иконка из шаблонов и меню есть в собранном app/static/icons.svg (иначе на кнопке пусто)."""
import importlib.util
import re
from pathlib import Path

from app.web import INFRA_TABS, MENU

ROOT = Path(__file__).resolve().parents[1]


def _builder():
    spec = importlib.util.spec_from_file_location("build_icons", ROOT / "tools" / "build_icons.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_used_icons_are_built():
    have = set(re.findall(r'<symbol viewBox="0 0 24 24" id="([a-z0-9-]+)"', (ROOT / "app/static/icons.svg").read_text()))
    need = _builder().used_in_templates() | {m[1] for m in MENU + INFRA_TABS}
    assert need - have == set(), "запустите .venv/bin/python tools/build_icons.py"


def test_icons_svg_is_up_to_date():
    assert (ROOT / "app/static/icons.svg").read_text() == _builder().build()
