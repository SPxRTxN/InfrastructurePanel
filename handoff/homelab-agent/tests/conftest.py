"""Общие помощники тестов: фейковые деревья /proc и /sys во временном каталоге."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
# Пакет импортируется и при запуске из другого корня (например, вместе с тестами дашборда)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


class FakeStatvfs:
    """os.statvfs_result-подобный объект."""

    def __init__(self, blocks, bfree, bavail, frsize=4096):
        self.f_blocks, self.f_bfree, self.f_bavail, self.f_frsize = blocks, bfree, bavail, frsize


@pytest.fixture
def pi_root(tmp_path):
    """Дерево как на Raspberry Pi 4B."""
    proc = make_tree(tmp_path / "proc", {
        "stat": "cpu  1000 0 500 8000 500 0 0 0 0 0\ncpu0 250 0 125 2000 125 0 0 0 0 0\n",
        "loadavg": "0.52 0.41 0.30 1/180 4242\n",
        "meminfo": fixture_text("meminfo_pi.txt"),
        "uptime": "86461.33 300000.10\n",
        "self/mounts": fixture_text("mounts_pi.txt"),
    })
    sys_ = make_tree(tmp_path / "sys", {
        "class/thermal/thermal_zone0/type": "cpu-thermal\n",
        "class/thermal/thermal_zone0/temp": "51234\n",
        "class/hwmon/hwmon0/name": "cpu_thermal\n",
        "class/hwmon/hwmon0/temp1_input": "51234\n",
        "class/hwmon/hwmon1/name": "rpi_volt\n",
        "class/hwmon/hwmon1/in0_lcrit_alarm": "0\n",
        "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "1500000\n",
    })
    return {"proc_root": str(proc), "sys_root": str(sys_)}
