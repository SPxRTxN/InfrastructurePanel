"""Без /proc (Windows, BSD) базовые коллекторы берут данные из psutil — те же id датчиков."""
from homelab_agent.collectors.base import build_collectors, safe_read
from homelab_agent.collectors.disks import psutil_mounts, VIRTUAL_FS, _mount_path

NO_PROC = {"proc_root": "/nonexistent-proc", "sys_root": "/nonexistent-sys"}


def test_basic_collectors_without_proc():
    collectors = build_collectors({name: dict(NO_PROC) for name in ("cpu", "memory", "disks", "uptime")}, 10)
    assert all(c.use_psutil for c in collectors)
    values = {}
    for c in collectors:
        values.update(safe_read(c))
    assert values["memory.total"] > 0 and values["memory.used"] + values["memory.available"] == values["memory.total"]
    assert 0 <= values["cpu.percent"] <= 100
    assert values["uptime.seconds"] > 0
    disks = [k for k in values if k.startswith("disks.") and k.endswith(".total")]
    assert disks, "psutil не нашёл ни одного раздела"
    ids = {s.id for c in collectors for s in c.sensors()}
    assert set(disks) <= ids


def test_psutil_mounts_skip_virtual_and_dedupe(monkeypatch):
    class P:
        def __init__(self, device, mountpoint, fstype, opts="rw"):
            self.device, self.mountpoint, self.fstype, self.opts = device, mountpoint, fstype, opts

    parts = [P("C:\\", "C:\\", "NTFS"), P("D:\\", "D:\\", "", "cdrom"), P("E:\\", "E:\\", "NTFS"),
             P("tmp", "/tmp", "tmpfs")]
    monkeypatch.setattr("psutil.disk_partitions", lambda all=False: parts)
    assert psutil_mounts(VIRTUAL_FS) == [("C:\\", "C:", "NTFS"), ("E:\\", "E:", "NTFS")]
    assert _mount_path("C:") == "C:\\" and _mount_path("/") == "/"
