#!/usr/bin/env python3
"""Сборка файлов релиза: архивы агента и нативной установки панели, SHA256SUMS, текст релиза.

    python tools/build_release.py --out dist            # версия — из app/__init__.py
    python tools/build_release.py --out dist --notes dist/notes.md

Архивы детерминированные: файлы отсортированы, mtime=0, uid/gid=0 без имён, права 0644/0755,
gzip без метки времени — одинаковые исходники дают одинаковые байты и хеши.
"""
import argparse
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = ROOT / "handoff" / "homelab-agent"

# Агент: как handoff/homelab-agent/push-agent — без окружения, тестов, заметок разработки и секретов
AGENT_EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", "tests", "Obsidian", ".git"}
AGENT_EXCLUDE_NAMES = {"agent.toml", "push-agent"}
AGENT_EXCLUDE_PREFIX = ("token-",)

# Панель: то, что копирует deploy/native/install.sh, и только оно (белый список — секреты не попадут)
NATIVE_DIRS = ["app", "migrations", "tools", "deploy/native"]
NATIVE_FILES = ["alembic.ini", "requirements.txt", "requirements.lock", "README.md"]
NATIVE_EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "tests", ".venv"}
NATIVE_EXCLUDE_SUFFIX = (".pyc", ".db", ".sqlite", ".key", ".pem", ".age")
NATIVE_EXCLUDE_NAMES = {".env"}

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _version_in(path: Path) -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit(f"в {path} не найден __version__")
    return m.group(1)


def read_version(root: Path = ROOT) -> str:
    """Версия панели (тег релиза vX.Y.Z)."""
    return _version_in(root / "app" / "__init__.py")


def read_agent_version(agent_dir: Path = AGENT_DIR) -> str:
    """У агента своя версия: по ней панель предлагает узлам обновление, по ней же названы его архивы."""
    return _version_in(agent_dir / "homelab_agent" / "__init__.py")


def changelog_section(version: str, root: Path = ROOT, name: str = "CHANGELOG.md") -> str:
    """Текст раздела «## X.Y.Z — дата» из CHANGELOG (без заголовка)."""
    text = (root / name).read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(version)}\b.*?$\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m or not m.group(1).strip():
        raise SystemExit(f"в {root / name} нет раздела «## {version} — дата» или он пуст")
    return m.group(1).strip() + "\n"


def release_notes(version: str, root: Path = ROOT) -> str:
    """Текст релиза: панель (Docker и нативная установка) и агент — со своими версиями и разделами CHANGELOG."""
    agent_root = root / "handoff" / "homelab-agent"
    agent_version = read_agent_version(agent_root)
    # В Actions — репозиторий, где идёт сборка (у форка свой образ)
    image = "ghcr.io/" + os.getenv("GITHUB_REPOSITORY", "SPxRTxN/InfrastructurePanel").lower()
    return (f"# Панель {version}\n\nDocker-образ `{image}:{version}` (amd64, arm64) и архив "
            f"нативной установки "
            f"`homelab-dashboard-native-{version}.tar.gz`.\n\n{changelog_section(version, root)}\n"
            f"# Агент {agent_version}\n\nАрхивы `homelab-agent-{agent_version}.tar.gz` (Linux) и `.zip` (Windows); "
            f"та же версия встроена в образ панели — узлам предлагается обновление в «Инфраструктура → Агенты».\n\n"
            f"{changelog_section(agent_version, agent_root)}")


def _walk(base: Path, rel: str, skip_dirs: set[str], skip_names: set[str], skip_suffix=(), skip_prefix=()):
    """Файлы каталога base/rel (пути относительно base), отсортированные."""
    top = base / rel
    out = []
    for p in sorted(top.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        parts = p.relative_to(base).parts
        if any(x in skip_dirs for x in parts[:-1]):
            continue
        name = parts[-1]
        if name in skip_names or name.endswith(tuple(skip_suffix)) or name.startswith(tuple(skip_prefix)):
            continue
        out.append("/".join(parts))
    return out


def agent_files() -> list[str]:
    return _walk(AGENT_DIR, ".", AGENT_EXCLUDE_DIRS, AGENT_EXCLUDE_NAMES, (".pyc",), AGENT_EXCLUDE_PREFIX)


def native_files(root: Path = ROOT) -> list[str]:
    files = [f for f in NATIVE_FILES if (root / f).is_file()]
    for d in NATIVE_DIRS:
        files += _walk(root, d, NATIVE_EXCLUDE_DIRS, NATIVE_EXCLUDE_NAMES, NATIVE_EXCLUDE_SUFFIX)
    return sorted(set(files))


def _mode(path: Path) -> int:
    return 0o755 if path.stat().st_mode & 0o111 else 0o644


def write_tar(dest: Path, base: Path, files: list[str], prefix: str) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in files:
            src = base / rel
            info = tarfile.TarInfo(f"{prefix}/{rel}")
            info.size = src.stat().st_size
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = _mode(src)
            with src.open("rb") as fh:
                tar.addfile(info, fh)
    with dest.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(buf.getvalue())


def write_zip(dest: Path, base: Path, files: list[str], prefix: str) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in files:
            src = base / rel
            info = zipfile.ZipInfo(f"{prefix}/{rel}", date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (0o100000 | _mode(src)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, src.read_bytes())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(out: Path, version: str, root: Path = ROOT) -> list[Path]:
    """Собирает архивы и SHA256SUMS в out; возвращает пути файлов релиза."""
    if not SEMVER.match(version):
        raise SystemExit(f"версия «{version}» не вида X.Y.Z")
    out.mkdir(parents=True, exist_ok=True)
    agent = agent_files()
    if not agent:
        raise SystemExit(f"нет файлов агента в {AGENT_DIR}")
    av = read_agent_version()
    if not SEMVER.match(av):
        raise SystemExit(f"версия агента «{av}» не вида X.Y.Z")
    names = [
        (out / f"homelab-agent-{av}.tar.gz", lambda p: write_tar(p, AGENT_DIR, agent, "homelab-agent")),
        (out / f"homelab-agent-{av}.zip", lambda p: write_zip(p, AGENT_DIR, agent, "homelab-agent")),
        (out / f"homelab-dashboard-native-{version}.tar.gz",
         lambda p: write_tar(p, root, native_files(root), f"homelab-dashboard-{version}")),
    ]
    for path, make in names:
        make(path)
    files = [p for p, _ in names]
    sums = out / "SHA256SUMS"
    sums.write_text("".join(f"{sha256(p)}  {p.name}\n" for p in sorted(files)), encoding="utf-8")
    return files + [sums]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    ap.add_argument("--version", default=None, help="по умолчанию — __version__ из app/__init__.py")
    ap.add_argument("--notes", type=Path, help="записать сюда раздел CHANGELOG для этой версии")
    args = ap.parse_args()
    version = args.version or read_version()
    if args.notes:
        args.notes.parent.mkdir(parents=True, exist_ok=True)
        args.notes.write_text(release_notes(version), encoding="utf-8")
    for p in build(args.out, version):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
