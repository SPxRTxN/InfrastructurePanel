"""Экран uptime: время работы узла и агента, последнее действие восстановления."""
from datetime import datetime

from ..base import screen
from . import human_duration


@screen("uptime")
def draw(ctx):
    st = ctx.state
    ctx.text(0, 0, "Up " + human_duration(ctx.value("uptime.seconds")), big=True)
    if st.agent_started:
        ctx.text(0, 20, "Agent " + human_duration(st.now - st.agent_started))
    last = st.last_action
    if last:
        when = datetime.fromtimestamp(last["ts"]).strftime("%d.%m %H:%M")
        ctx.text(0, 34, f"{last['name']}")
        ctx.text(0, 47, f"{'OK' if last['ok'] else 'FAIL'} {when}")
    else:
        ctx.text(0, 34, "No actions")
