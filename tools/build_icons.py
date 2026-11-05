"""Собирает app/static/icons.svg из полного спрайта Lucide — только нужные иконки.

Запуск: .venv/bin/python tools/build_icons.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app/static/vendor/lucide-sprite.svg"
DST = ROOT / "app/static/icons.svg"
TEMPLATES = ROOT / "app/templates"
USE = re.compile(r'icon\("([a-z0-9-]+)"')


def used_in_templates() -> set[str]:
    """Все icon("…") из шаблонов — новую иконку не нужно вписывать сюда вручную (тест test_icons это проверяет)."""
    return {m for p in TEMPLATES.rglob("*.html") for m in USE.findall(p.read_text(encoding="utf-8"))}


# Иконки, которые подставляются не литералом в шаблоне (из Python, JS, переменных)
ICONS = """
house server square-kanban book-open calendar network settings log-out bell user users
panel-left-close panel-left-open search plus x check chevron-down chevron-right trash-2 pencil
link file-text file-plus hard-drive memory-stick cpu activity wifi wifi-off circle-check circle-x
clock eye save history router monitor cloud globe maximize-2 folder refresh-cw triangle-alert filter
arrow-left list shield palette layout-dashboard external-link grip-vertical sun moon radio download
lightbulb laptop tag flag calendar-clock repeat boxes
""".split()


def build() -> str:
    text = SRC.read_text()
    out = []
    for name in sorted(set(ICONS) | used_in_templates()):
        m = re.search(rf'<symbol id="{re.escape(name)}">.*?</symbol>', text, re.S)
        if not m:
            raise SystemExit(f"нет иконки {name}")
        # viewBox нужен, чтобы иконка масштабировалась при использовании через <use>
        out.append(m.group(0).replace("<symbol ", '<symbol viewBox="0 0 24 24" ', 1))
    return '<svg xmlns="http://www.w3.org/2000/svg"><defs>\n' + "\n".join(out) + "\n</defs></svg>\n"


if __name__ == "__main__":
    svg = build()
    DST.write_text(svg)
    print(f"{svg.count('<symbol ')} иконок → {DST.relative_to(ROOT)}")
