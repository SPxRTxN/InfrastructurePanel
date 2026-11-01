"""Агент, встроенный в образ панели, регистрируется в каталоге версий: обновил панель — узлам доступно обновление."""
from pathlib import Path

from sqlalchemy import func, select

from app.models import AgentRelease
from app.services import agent_updates

AGENT = Path(__file__).resolve().parents[1] / "handoff" / "homelab-agent"


def test_register_bundled_once(db, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_updates, "releases_dir", lambda: tmp_path / "rel")
    release = agent_updates.register_bundled(db, AGENT)
    assert release and release.notes.startswith("встроено в панель")
    assert f'"{release.version}"' in (AGENT / "homelab_agent" / "__init__.py").read_text()  # версия — из исходников
    assert agent_updates.register_bundled(db, AGENT) is None  # второй раз — не дублирует
    assert db.scalar(select(func.count()).select_from(AgentRelease)) == 1


def test_pack_is_deterministic_and_clean(monkeypatch):
    import time

    a = agent_updates.pack_bundle(AGENT)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3600)  # другое «сейчас» — архив тот же
    b = agent_updates.pack_bundle(AGENT)
    assert a == b
    import io
    import tarfile

    names = tarfile.open(fileobj=io.BytesIO(a)).getnames()
    assert "homelab_agent/__init__.py" in names
    assert not any(n.startswith(("tests/", "Obsidian/")) or "__pycache__" in n for n in names)


def test_no_bundle_outside_image(db, tmp_path):
    assert agent_updates.register_bundled(db, tmp_path / "nope") is None


def test_dockerfile_bundles_agent():
    root = Path(__file__).resolve().parents[1]
    assert "COPY handoff/homelab-agent ./agent-bundle" in (root / "Dockerfile").read_text()
    ignore = (root / ".dockerignore").read_text()
    assert "!handoff/homelab-agent/" in ignore and "handoff/homelab-agent/tests/" in ignore
