"""Экран panels: к каким панелям подключён; сводка «узлов онлайн/всего» и тревога, если панель прислала summary."""
from ..base import screen


@screen("panels")
def draw(ctx):
    st = ctx.state
    if not st.panels:
        ctx.text(0, 0, "No panels", big=True)
        ctx.text(0, 20, "standalone mode")
        return
    y = 0
    for name, connected in st.panels[:3]:
        ctx.text(0, y, ("+ " if connected else "x ") + name)
        y += 12
    s = st.summary
    if s and s.get("total") is not None:
        ctx.text(0, 38, f"Nodes {s.get('online', '?')}/{s['total']}")
    if s and s.get("alert"):
        ctx.text(0, 51, "! " + s["alert"])
