"""Страница «Настройки → Обновление»: без цикла перезагрузок, список релизов GitHub, состояние службы обновления."""
import json
import urllib.error

import pytest

from app import __version__
from app.services import maintenance, releases


@pytest.fixture
def upd(tmp_path, monkeypatch):
    d = tmp_path / "update"
    monkeypatch.setattr(maintenance, "update_dir", lambda: d)
    return d


def test_idle_page_shows_start_form(admin, upd):
    """Регрессия: «active» перетирался подсветкой меню — форма пряталась, update.js по кругу перезагружал страницу."""
    page = admin.get("/settings/update").text
    assert 'action="/settings/update/start"' in page and "Обновление не запущено" in page
    assert admin.get("/settings/update/status").json()["active"] is False


def _gh(version, **kw):
    return {"tag_name": f"v{version}", "published_at": "2026-11-01T10:00:00Z", "body": f"## {version}\n- новое",
            "draft": False, "prerelease": False, **kw}


def test_releases_listed_with_newer_and_current(admin, upd, monkeypatch):
    major, minor, patch = (int(x) for x in __version__.split("."))
    newer = f"{major}.{minor}.{patch + 1}"
    monkeypatch.setattr(releases, "fetch", lambda repo: [_gh(__version__), _gh(newer), _gh("9.9.9", draft=True),
                                                         {"tag_name": "nightly"}])
    page = admin.get("/settings/update").text
    assert newer in page and "новее текущей" in page and "установлена" in page
    assert "9.9.9" not in page  # черновики не показываем
    assert f'name="version" value="{newer}"' in page  # кнопка «Обновить до …»


def test_releases_cached_and_refresh(admin, upd, monkeypatch):
    calls = []
    monkeypatch.setattr(releases, "fetch", lambda repo: calls.append(repo) or [])
    admin.get("/settings/update")
    admin.get("/settings/update")
    assert len(calls) == 1  # кэш 10 минут
    admin.post("/settings/update/releases/refresh")
    assert len(calls) == 2


def test_release_errors_are_explained(admin, upd, monkeypatch):
    def private(repo):
        raise urllib.error.HTTPError("u", 404, "nf", {}, None)
    monkeypatch.setattr(releases, "fetch", private)
    assert "Репозиторий не найден или закрыт" in admin.get("/settings/update").text
    releases._cache.update(at=0.0, data=None)

    def offline(repo):
        raise OSError("no route")
    monkeypatch.setattr(releases, "fetch", offline)
    assert "Не удалось связаться с GitHub" in admin.get("/settings/update").text


def test_updater_status(admin, upd):
    assert "служба обновления не обнаружена" in admin.get("/settings/update").text
    upd.mkdir()
    (upd / "updater.json").write_text(json.dumps({"installed_at": "2026-11-01 08:00"}))
    page = admin.get("/settings/update").text
    assert "служба обновления установлена" in page and "2026-11-01 08:00" in page


def test_installer_writes_marker():
    from pathlib import Path

    sh = (Path(__file__).resolve().parents[1] / "tools" / "install-updater.sh").read_text()
    assert "updater.json" in sh and "installed_at" in sh


def test_only_settings_manage(user):
    assert user.get("/settings/update").status_code == 403
    assert user.post("/settings/update/releases/refresh").status_code == 403
