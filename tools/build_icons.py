"""Собирает app/static/icons.svg из полного спрайта Lucide — только нужные иконки.

Запуск: .venv/bin/python tools/build_icons.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app/static/vendor/lucide-sprite.svg"
DST = ROOT / "app/static/icons.svg"

ICONS = """
house server square-kanban book-open calendar network settings log-out bell user users
panel-left-close panel-left-open search plus x check chevron-down chevron-right trash-2 pencil
link file-text file-plus hard-drive memory-stick cpu activity wifi wifi-off circle-check circle-x
clock eye save history router monitor cloud globe folder refresh-cw triangle-alert filter
arrow-left list shield palette layout-dashboard external-link grip-vertical sun moon radio
lightbulb laptop tag flag calendar-clock repeat
""".split()

text = SRC.read_text()
out = []
for name in ICONS:
    m = re.search(rf'<symbol id="{re.escape(name)}">.*?</symbol>', text, re.S)
    if not m:
        raise SystemExit(f"нет иконки {name}")
    # viewBox нужен, чтобы иконка масштабировалась при использовании через <use>
    out.append(m.group(0).replace("<symbol ", '<symbol viewBox="0 0 24 24" ', 1))

DST.write_text('<svg xmlns="http://www.w3.org/2000/svg"><defs>\n' + "\n".join(out) + "\n</defs></svg>\n")
print(f"{len(out)} иконок → {DST.relative_to(ROOT)}")
