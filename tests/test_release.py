"""Версия приложения и сборка релиза (tools/build_release.py, .github/workflows/release.yml)."""
import hashlib
import importlib.util
import re
import tarfile
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import __version__
from app.main import app

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
FORBIDDEN = ("/tests/", "/Obsidian/", "/.venv/", "__pycache__", ".env", "agent.toml", "/token-", "/secrets/", "/data/")


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools" / "build_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _names(path: Path) -> list[str]:
    if path.suffix == ".zip":
        return zipfile.ZipFile(path).namelist()
    with tarfile.open(path) as tar:
        return tar.getnames()


def test_version_is_semver():
    assert SEMVER.match(__version__)


def test_healthz_reports_version():
    body = TestClient(app).get("/healthz").json()
    assert body == {"status": "ok", "version": __version__}


def test_changelog_has_current_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## {re.escape(__version__)} — \d{{4}}-\d{{2}}-\d{{2}}$", text, re.M)
    assert "Как обновиться" in text
    assert _load_builder().changelog_section(__version__).strip()


def test_build_release(tmp_path):
    b = _load_builder()
    files = b.build(tmp_path / "out", __version__)
    av = b.read_agent_version()
    names = {p.name for p in files}
    # Агент — со своей версией, панель (нативная) — с версией панели
    assert names == {f"homelab-agent-{av}.tar.gz", f"homelab-agent-{av}.zip",
                     f"homelab-dashboard-native-{__version__}.tar.gz", "SHA256SUMS"}

    for p in files:
        if p.name == "SHA256SUMS":
            continue
        listed = _names(p)
        assert listed, p.name
        for n in listed:
            assert not any(bad in "/" + n for bad in FORBIDDEN), (p.name, n)

    agent = _names(tmp_path / "out" / f"homelab-agent-{av}.tar.gz")
    assert "homelab-agent/homelab_agent/__init__.py" in agent
    assert "homelab-agent/start-agent" in agent
    native = _names(tmp_path / "out" / f"homelab-dashboard-native-{__version__}.tar.gz")
    root = f"homelab-dashboard-{__version__}"
    for need in ("deploy/native/install.sh", "deploy/native/homelab-dashboard.service", "app/cli.py",
                 "migrations/env.py", "alembic.ini", "requirements.txt", "requirements.lock",
                 "tools/setup_secrets.py", "README.md"):
        assert f"{root}/{need}" in native, need

    # SHA256SUMS совпадает с файлами
    lines = (tmp_path / "out" / "SHA256SUMS").read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        digest, name = line.split("  ")
        assert hashlib.sha256((tmp_path / "out" / name).read_bytes()).hexdigest() == digest


def test_release_notes_cover_panel_and_agent():
    b = _load_builder()
    notes = b.release_notes(__version__)
    av = b.read_agent_version()
    assert f"# Панель {__version__}" in notes and f"# Агент {av}" in notes
    assert b.changelog_section(av, b.AGENT_DIR).strip() in notes


def test_build_is_deterministic(tmp_path):
    b = _load_builder()
    b.build(tmp_path / "a", __version__)
    b.build(tmp_path / "b", __version__)
    assert (tmp_path / "a" / "SHA256SUMS").read_text() == (tmp_path / "b" / "SHA256SUMS").read_text()
    with tarfile.open(tmp_path / "a" / f"homelab-agent-{b.read_agent_version()}.tar.gz") as tar:
        for m in tar.getmembers():
            assert (m.mtime, m.uid, m.gid, m.uname, m.gname) == (0, 0, 0, "", "")
        assert tar.getnames() == sorted(tar.getnames())


def test_release_workflow():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml:
        wf = yaml.safe_load(text)
        triggers = wf.get("on", wf.get(True))
        assert triggers["push"]["tags"] == ["v*.*.*"]
        assert {"test", "image", "release"} <= set(wf["jobs"])
    else:
        assert 'tags: ["v*.*.*"]' in text
    # Проверка совпадения тега с __version__ и привязка шагов к тестам
    assert "не совпадает с версией приложения" in text
    assert "tools/build_release.py" in text
    assert "linux/amd64,linux/arm64" in text
    assert "pytest -q" in text and "SHA256SUMS" in text
    # Ручной запуск для уже отправленного тега; ввод попадает в shell только через env, не подстановкой в run
    assert "workflow_dispatch" in text and "inputs.tag || github.ref_name" in text
    run_blocks = re.findall(r"run: \|\n((?:\s{10,}.*\n)+)", text)
    assert run_blocks and not any("inputs." in b or "RELEASE_TAG }}" in b for b in run_blocks)
    assert text.count("ref: refs/tags/${{ env.RELEASE_TAG }}") == 3  # все задачи собирают код тега
