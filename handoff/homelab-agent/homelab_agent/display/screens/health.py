"""Экран health: температура CPU, загрузка, RAM, диск /."""
from ..base import screen
from . import human_bytes


@screen("health")
def draw(ctx):
    temp = ctx.value("thermal.cpu.temp")
    cpu = ctx.value("cpu.percent")
    load = ctx.value("cpu.load1")
    used, total = ctx.value("memory.used"), ctx.value("memory.total")
    d_used, d_total = ctx.value("disks./.used"), ctx.value("disks./.total")

    line1 = "CPU " + (f"{temp:.0f}C" if temp is not None else "--")
    if cpu is not None:
        line1 += f"  {cpu:.0f}%"
    ctx.text(0, 0, line1, big=True)
    ctx.text(0, 18, "Load " + (f"{load:.2f}" if load is not None else "--"))
    ram = f"RAM {human_bytes(used)}/{human_bytes(total)}" if total else "RAM --"
    if total:
        ram += f" {100 * used / total:.0f}%"
    ctx.text(0, 31, ram)
    disk = f"/ {human_bytes(d_used)}/{human_bytes(d_total)}" if d_total else "/ --"
    if d_total:
        disk += f" {100 * d_used / d_total:.0f}%"
    ctx.text(0, 44, disk)
