"""Экран network: имя узла и IP основного интерфейса (главное, ради чего дисплей стоит на Pi)."""
from ..base import screen


@screen("network")
def draw(ctx):
    st = ctx.state
    ctx.text(0, 0, st.hostname or "?", big=True)
    ctx.draw.line((ctx.dx, 19 + ctx.dy, ctx.width - 1, 19 + ctx.dy), fill=1)
    ctx.text(0, 23, st.ip or "no IP", big=True)
    if st.iface:
        ctx.text(0, 42, st.iface)
