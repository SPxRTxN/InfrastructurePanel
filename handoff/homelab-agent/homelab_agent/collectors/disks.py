"""Диски: по каждой реальной ФС — размер / занято / свободно в байтах (как `df -B1`).

Список монтирований — /proc/self/mounts, размеры — os.statvfs.
Отсекаются виртуальные ФС, снапы (loop-устройства, /snap/*) и повторы одного
устройства (bind-монтирования, подтома btrfs, файлы Docker) — остаётся самая
короткая точка монтирования.
"""
from __future__ import annotations

import os
import re

import psutil

from .base import Collector, Sensor, register

# Виртуальные и служебные ФС — не диски
VIRTUAL_FS = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2",
    "devpts", "mqueue", "debugfs", "tracefs", "securityfs", "pstore", "bpf", "configfs",
    "fusectl", "hugetlbfs", "autofs", "binfmt_misc", "ramfs", "efivarfs", "nsfs",
    "rpc_pipefs", "nfsd", "selinuxfs", "fuse.lxcfs", "fuse.gvfsd-fuse", "fuse.portal",
    "fuse.snapfuse", "zram", "aufs", "shm",
}
# ФС, у которых «устройство» не путь в /dev, но это настоящие тома
NON_DEV_REAL_FS = {"zfs", "nfs", "nfs4", "cifs", "smb3", "9p", "virtiofs"}

_OCTAL = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    # В /proc/mounts пробел записан как \040, таб — \011 и т.д.
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mounts(text: str, exclude_fs: set[str]) -> list[tuple[str, str, str]]:
    """→ [(устройство, точка монтирования, fs)] только реальных ФС, без повторов устройства."""
    by_device: dict[str, tuple[str, str, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fs = _unescape(parts[0]), _unescape(parts[1]), parts[2]
        if fs in exclude_fs:
            continue
        if not dev.startswith("/dev/") and fs not in NON_DEV_REAL_FS:
            continue
        if dev.startswith("/dev/loop") or mnt.startswith(("/snap/", "/var/lib/snapd/")):
            continue  # снапы и образы
        prev = by_device.get(dev)
        if prev is None or (len(mnt), mnt) < (len(prev[1]), prev[1]):
            by_device[dev] = (dev, mnt, fs)
    return sorted(by_device.values(), key=lambda x: (x[1] != "/", x[1]))


def _mount_path(mnt: str) -> str:
    # «C:» в идентификаторе датчика → «C:\» для запроса размера
    return mnt + "\\" if len(mnt) == 2 and mnt[1] == ":" else mnt


def psutil_mounts(exclude_fs: set[str]) -> list[tuple[str, str, str]]:
    """Без /proc (Windows, BSD): разделы из psutil. Пустые приводы и CD-ROM пропускаются."""
    by_device: dict[str, tuple[str, str, str]] = {}
    for p in psutil.disk_partitions(all=False):
        if not p.fstype or p.fstype.lower() in exclude_fs or "cdrom" in (p.opts or ""):
            continue
        mnt = p.mountpoint.rstrip("\\") if len(p.mountpoint) > 1 else p.mountpoint
        prev = by_device.get(p.device)
        if prev is None or len(mnt) < len(prev[1]):
            by_device[p.device] = (p.device, mnt, p.fstype)
    return sorted(by_device.values(), key=lambda x: (x[1] not in ("/", "C:"), x[1]))


@register("disks")
class DisksCollector(Collector):
    KEYS = {"exclude_fs", "include_mounts"}

    # Подменяется в тестах; в Windows os.statvfs нет — там путь через psutil
    statvfs = staticmethod(getattr(os, "statvfs", None))

    def __init__(self, cfg=None, default_interval=10):
        super().__init__(cfg, default_interval)
        extra = self.cfg.get("exclude_fs", [])
        if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
            raise ValueError("[collectors.disks]: exclude_fs — список строк")
        self.exclude_fs = VIRTUAL_FS | set(extra)
        only = self.cfg.get("include_mounts")
        if only is not None and (not isinstance(only, list) or not all(isinstance(x, str) for x in only)):
            raise ValueError("[collectors.disks]: include_mounts — список точек монтирования")
        self.include = set(only) if only else None

    def _mounts(self):
        if self.use_psutil:
            mounts = psutil_mounts(self.exclude_fs)
        else:
            mounts = parse_mounts(self._read_text(f"{self.proc}/self/mounts") or "", self.exclude_fs)
        if self.include is not None:
            mounts = [m for m in mounts if m[1] in self.include]
        return mounts

    def sensors(self):
        out = []
        for dev, mnt, fs in self._mounts():
            label = f"{dev} {fs}"
            for metric in ("total", "used", "free"):
                out.append(Sensor(f"disks.{mnt}.{metric}", "disk", label, "B"))
        return out

    def read(self):
        values = {}
        for _dev, mnt, _fs in self._mounts():
            if self.use_psutil:
                try:
                    u = psutil.disk_usage(_mount_path(mnt))
                except OSError:
                    continue
                values[f"disks.{mnt}.total"], values[f"disks.{mnt}.used"], values[f"disks.{mnt}.free"] = (
                    u.total, u.used, u.free)
                continue
            try:
                st = self.statvfs(mnt)
            except OSError:
                continue  # нет прав или том отвалился — остальные диски читаем
            if st.f_blocks == 0:
                continue
            values[f"disks.{mnt}.total"] = st.f_blocks * st.f_frsize
            values[f"disks.{mnt}.used"] = (st.f_blocks - st.f_bfree) * st.f_frsize
            # Как «Avail» в df: место, доступное обычному пользователю
            values[f"disks.{mnt}.free"] = st.f_bavail * st.f_frsize
        return values
