"""Руководство в панели: Markdown из app/guide/*.md → пространство GUIDE в Confluence.

Источник правды — файлы в коде. Управляемая страница помечена скрытой строкой в конце текста
(`[//]: # (homelab-guide:<файл>:<хеш>)` — определение ссылки Markdown, при показе не выводится).
Идемпотентность: страница обновляется, только если хеш исходника в метке не совпал с текущим.
Правка управляемой страницы в панели сохраняется как обычная версия (история Confluence её хранит),
но пока исходник не менялся, синхронизация правку не трогает; изменился исходник — страница
перезаписывается новой версией.
"""
import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Page, PageVersion, Space
from app.utils import utcnow

log = logging.getLogger("uvicorn.error")

GUIDE_DIR = Path(__file__).resolve().parent.parent / "guide"
SPACE_KEY = "GUIDE"
SPACE_NAME = "Руководство"
SPACE_DESCRIPTION = "Руководство по панели. Обновляется из кода автоматически."
ROOT_FILE = "00-Руководство"

_MARK_RE = re.compile(r"^\[//\]: # \(homelab-guide:([^:)]+):([0-9a-f]+)\)\s*$", re.M)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)


def _mark(key: str, digest: str) -> str:
    return f"[//]: # (homelab-guide:{key}:{digest})"


def _sources() -> list[tuple[str, str, str, str]]:
    """[(ключ = имя файла без .md, заголовок, текст, хеш)] в порядке имён файлов."""
    out = []
    for path in sorted(GUIDE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        m = _H1_RE.search(text)
        out.append((path.stem, m.group(1) if m else path.stem, text,
                    hashlib.sha256(text.encode()).hexdigest()[:16]))
    return out


def _find(pages: list[Page], key: str, title: str) -> Page | None:
    for p in pages:
        m = _MARK_RE.search(p.body)
        if m and m.group(1) == key:
            return p
    # Метку могли стереть при правке — тогда ищем по названию, чтобы не плодить дубли
    return next((p for p in pages if p.title == title), None)


def sync(db: Session) -> dict[str, int]:
    """Создаёт/обновляет страницы руководства. → {created, updated, unchanged}."""
    stats = {"created": 0, "updated": 0, "unchanged": 0}
    sources = _sources()
    if not sources:
        return stats
    space = db.scalar(select(Space).where(Space.key == SPACE_KEY))
    if not space:
        space = Space(key=SPACE_KEY, name=SPACE_NAME, description=SPACE_DESCRIPTION)
        db.add(space)
        db.flush()
    pages = list(db.scalars(select(Page).where(Page.space_id == space.id)))
    root_id: int | None = None
    for key, title, text, digest in sources:
        body = f"{text}\n\n{_mark(key, digest)}\n"
        parent_id = None if key == ROOT_FILE else root_id
        page = _find(pages, key, title)
        if page is None:
            page = Page(space_id=space.id, parent_id=parent_id, title=title, body=body)
            db.add(page)
            db.flush()
            db.add(PageVersion(page_id=page.id, version=page.version, title=title, body=body))
            pages.append(page)
            stats["created"] += 1
        else:
            m = _MARK_RE.search(page.body)
            if m and m.group(1) == key and m.group(2) == digest:
                stats["unchanged"] += 1
            else:
                page.title, page.body, page.parent_id = title, body, parent_id
                page.version, page.author_id, page.updated_at = page.version + 1, None, utcnow()
                db.add(PageVersion(page_id=page.id, version=page.version, title=title, body=body))
                stats["updated"] += 1
        if key == ROOT_FILE:
            root_id = page.id
    db.commit()
    return stats


def sync_safely() -> None:
    """Для старта приложения: ошибка руководства не должна мешать запуску."""
    from app.db import SessionLocal

    try:
        with SessionLocal() as db:
            stats = sync(db)
        log.info("Руководство: создано %(created)s, обновлено %(updated)s, без изменений %(unchanged)s", stats)
    except Exception:
        log.exception("Не удалось синхронизировать руководство")
