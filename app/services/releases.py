"""Доступные версии панели — релизы GitHub репозитория (то, что собирает release.yml по тегу vX.Y.Z).

Список кэшируется на 10 минут (и обновляется кнопкой «Проверить обновления»), чтобы не дёргать GitHub при каждом
открытии страницы. Сеть — только к api.github.com; в ответе берём лишь тег, дату, текст и признаки.
"""
import json
import re
import time
import urllib.error
import urllib.request

from app import __version__
from app.services.settings_registry import get_setting

TIMEOUT = 10
CACHE_SECONDS = 600
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")

_cache: dict = {"at": 0.0, "repo": "", "data": None}


def _key(version: str) -> tuple:
    return tuple(int(p) for p in version.split("."))


def fetch(repo: str) -> list[dict]:
    url = f"https://api.github.com/repos/{repo}/releases?per_page=15"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "homelab-dashboard"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # nosec B310 — фиксированный https api.github.com
        return json.loads(r.read(2 * 1024 * 1024))


def parse(raw: list, current: str = __version__) -> list[dict]:
    out = []
    for rel in raw if isinstance(raw, list) else []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        m = TAG_RE.match(str(rel.get("tag_name", "")))
        if not m:
            continue
        v = m.group(1)
        out.append({"version": v, "published": str(rel.get("published_at") or "")[:10],
                    "notes": str(rel.get("body") or "")[:20000], "prerelease": bool(rel.get("prerelease")),
                    "newer": _key(v) > _key(current) if TAG_RE.match(current) else False,
                    "current": v == current})
    return sorted(out, key=lambda r: _key(r["version"]), reverse=True)


def available(db, force: bool = False) -> dict:
    """{repo, versions, error, checked} — для страницы «Настройки → Обновление».
    Ключ не «items»: в Jinja `d.items` — метод словаря, а не значение."""
    repo = get_setting(db, "update.github_repo")
    if not REPO_RE.match(repo or ""):
        return {"repo": repo, "versions": [], "error": "Не задан репозиторий релизов (владелец/имя)", "checked": ""}
    now = time.time()
    if not force and _cache["repo"] == repo and _cache["data"] and now - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    error = ""
    versions: list[dict] = []
    try:
        versions = parse(fetch(repo))
    except urllib.error.HTTPError as e:
        error = ("Репозиторий не найден или закрыт — релизы не видны без доступа (сделайте его публичным)"
                 if e.code == 404 else f"GitHub ответил HTTP {e.code}")
    except (OSError, ValueError) as e:
        error = f"Не удалось связаться с GitHub ({e.__class__.__name__}) — проверьте доступ панели в интернет"
    data = {"repo": repo, "versions": versions, "error": error, "checked": time.strftime("%H:%M")}
    _cache.update(at=now, repo=repo, data=data)
    return data
