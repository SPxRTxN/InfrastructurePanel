import pytest

from conftest import FakeStatvfs, fixture_text, make_tree
from homelab_agent.collectors import Collector, Sensor, build_collectors, register, registry, safe_read
from homelab_agent.collectors.cpu import CpuCollector
from homelab_agent.collectors.disks import VIRTUAL_FS, DisksCollector, parse_mounts
from homelab_agent.collectors.memory import MemoryCollector
from homelab_agent.collectors.thermal import ThermalCollector
from homelab_agent.collectors.uptime import UptimeCollector


def test_registry_has_basic_collectors():
    reg = registry()
    for name in ("cpu", "memory", "disks", "thermal", "uptime"):
        assert name in reg


def test_unknown_key_rejected():
    with pytest.raises(ValueError, match="неизвестные ключи"):
        MemoryCollector({"bogus": 1})


def test_build_default_and_disable():
    names = [c.name for c in build_collectors(None, 10)]
    assert names == ["cpu", "memory", "disks", "thermal", "uptime"]
    names = [c.name for c in build_collectors({"cpu": {}, "memory": {"enabled": False}}, 10)]
    assert names == ["cpu"]
    with pytest.raises(ValueError, match="неизвестный коллектор"):
        build_collectors({"nope": {}}, 10)


def test_interval_per_collector():
    c = build_collectors({"cpu": {"interval": 30}, "memory": {}}, 10)
    assert [x.interval for x in c] == [30, 10]


def test_bad_kind_rejected():
    with pytest.raises(ValueError):
        Sensor("x.y", "Bad Kind", "x")


def test_safe_read_isolates_errors():
    @register("_broken_test")
    class Broken(Collector):
        def sensors(self):
            return []

        def read(self):
            raise RuntimeError("boom")

    assert safe_read(Broken()) == {}


def test_safe_read_drops_non_numbers():
    @register("_mixed_test")
    class Mixed(Collector):
        def sensors(self):
            return []

        def read(self):
            return {"a": 1, "b": float("nan"), "c": "x", "d": True, "e": 2.5}

    assert safe_read(Mixed()) == {"a": 1, "d": 1, "e": 2.5}


def test_memory(pi_root):
    c = MemoryCollector(pi_root)
    v = c.read()
    assert v["memory.total"] == 945904 * 1024
    assert v["memory.available"] == 541200 * 1024
    assert v["memory.used"] == (945904 - 541200) * 1024
    kinds = {s.id: (s.kind, s.unit) for s in c.sensors()}
    assert kinds == {k: ("memory", "B") for k in ("memory.total", "memory.used", "memory.available")}


def test_cpu_percent_delta_and_load(pi_root, tmp_path):
    c = CpuCollector(pi_root)
    first = c.read()
    # С загрузки: busy = 1000+500 = 1500, total = 10000
    assert first["cpu.percent"] == 15.0
    assert (first["cpu.load1"], first["cpu.load5"], first["cpu.load15"]) == (0.52, 0.41, 0.30)
    assert first["cpu.freq_mhz"] == 1500.0
    # За интервал: +300 busy, +1000 total → 30 %
    make_tree(tmp_path / "proc", {"stat": "cpu  1200 0 600 8600 600 0 0 0 0 0\n"})
    assert c.read()["cpu.percent"] == 30.0
    ids = [s.id for s in c.sensors()]
    assert ids == ["cpu.percent", "cpu.load1", "cpu.load5", "cpu.load15", "cpu.freq_mhz"]


def test_cpu_without_cpufreq(tmp_path):
    make_tree(tmp_path / "proc", {"stat": "cpu 1 0 1 8 0 0 0 0\n", "loadavg": "0 0 0 1/1 1\n"})
    c = CpuCollector({"proc_root": str(tmp_path / "proc"), "sys_root": str(tmp_path / "sys")})
    assert "cpu.freq_mhz" not in [s.id for s in c.sensors()]
    assert "cpu.freq_mhz" not in c.read()


def test_uptime(pi_root):
    assert UptimeCollector(pi_root).read() == {"uptime.seconds": 86461}


def test_parse_mounts_pi():
    m = parse_mounts(fixture_text("mounts_pi.txt"), VIRTUAL_FS)
    assert m == [
        ("/dev/mmcblk0p2", "/", "ext4"),
        ("/dev/mmcblk0p1", "/boot/firmware", "vfat"),
        ("/dev/sda1", "/mnt/usb disk", "ext4"),
    ]


def test_parse_mounts_server_dedup_snaps_overlay():
    m = parse_mounts(fixture_text("mounts_server.txt"), VIRTUAL_FS)
    assert [x[1] for x in m] == ["/", "/boot/efi", "/srv/data", "/tank/media"]
    assert ("tank/media", "/tank/media", "zfs") in m


def test_disks_collector_values(pi_root):
    c = DisksCollector(pi_root)
    sizes = {
        "/": FakeStatvfs(1000, 400, 350),
        "/boot/firmware": FakeStatvfs(100, 90, 90, frsize=512),
    }

    def fake(path):
        if path not in sizes:
            raise PermissionError(path)
        return sizes[path]

    c.statvfs = fake
    v = c.read()
    assert v == {
        "disks./.total": 1000 * 4096,
        "disks./.used": 600 * 4096,
        "disks./.free": 350 * 4096,
        "disks./boot/firmware.total": 100 * 512,
        "disks./boot/firmware.used": 10 * 512,
        "disks./boot/firmware.free": 90 * 512,
    }
    s = {x.id: x for x in c.sensors()}
    assert s["disks./.used"].label == "/dev/mmcblk0p2 ext4"
    assert s["disks./.used"].kind == "disk" and s["disks./.used"].unit == "B"
    assert "disks./mnt/usb disk.free" in s


def test_disks_exclude_and_include(pi_root):
    c = DisksCollector({**pi_root, "exclude_fs": ["vfat"]})
    assert not any("/boot/firmware" in s.id for s in c.sensors())
    c = DisksCollector({**pi_root, "include_mounts": ["/"]})
    assert {s.id for s in c.sensors()} == {"disks./.total", "disks./.used", "disks./.free"}
    with pytest.raises(ValueError):
        DisksCollector({"exclude_fs": "tmpfs"})


def test_disks_real_root_matches_statvfs():
    """На реальной машине значения для / совпадают с os.statvfs (как df)."""
    import os

    c = DisksCollector({})
    v = c.read()
    if "disks./.total" not in v:
        pytest.skip("корень — не реальная ФС (контейнер)")
    st = os.statvfs("/")
    assert v["disks./.total"] == st.f_blocks * st.f_frsize


def test_thermal_pi(pi_root):
    c = ThermalCollector(pi_root)
    assert c.read() == {"thermal.cpu.temp": 51.2}
    (s,) = c.sensors()
    assert (s.id, s.kind, s.unit) == ("thermal.cpu.temp", "temperature", "°C")


def test_thermal_hwmon_multi(tmp_path):
    sys_ = make_tree(tmp_path / "sys", {
        "class/thermal/thermal_zone0/type": "acpitz\n",
        "class/thermal/thermal_zone0/temp": "27800\n",
        "class/thermal/thermal_zone1/type": "x86_pkg_temp\n",
        "class/thermal/thermal_zone1/temp": "45000\n",
        "class/hwmon/hwmon1/name": "coretemp\n",
        "class/hwmon/hwmon1/temp1_input": "45000\n",
        "class/hwmon/hwmon1/temp1_label": "Package id 0\n",
        "class/hwmon/hwmon1/temp2_input": "43000\n",
        "class/hwmon/hwmon1/temp2_label": "Core 0\n",
        "class/hwmon/hwmon2/name": "nvme\n",
        "class/hwmon/hwmon2/temp1_input": "38850\n",
        "class/hwmon/hwmon3/name": "acpitz\n",
        "class/hwmon/hwmon3/temp1_input": "27800\n",
    })
    v = ThermalCollector({"sys_root": str(sys_)}).read()
    assert v == {
        "thermal.acpitz.temp": 27.8,
        "thermal.cpu.temp": 45.0,
        "thermal.coretemp_package_id_0.temp": 45.0,
        "thermal.coretemp_core_0.temp": 43.0,
        "thermal.nvme.temp": 38.9,
    }
