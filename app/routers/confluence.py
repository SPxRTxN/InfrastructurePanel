import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Issue, Page, PageVersion, Space, User
from app.routers import jira
from app.routers.home import kpi, widget
from app.utils import utcnow
from app.web import render, require

router = APIRouter()

SPACE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")


# ---------- Вспомогательное ----------

def _space(db: Session, key: str) -> Space:
    space = db.scalar(select(Space).where(Space.key == key.upper()))
    if not space:
        raise HTTPException(404, "Пространство не найдено")
    return space


def _page(db: Session, page_id: int) -> Page:
    page = db.get(Page, page_id)
    if not page:
        raise HTTPException(404, "Статья не найдена")
    return page


def build_tree(pages: list[Page]) -> list[dict]:
    """Дерево страниц пространства: [{page, children: [...]}]."""
    children: dict[int | None, list[Page]] = {}
    for p in pages:
        children.setdefault(p.parent_id, []).append(p)

    def node(p: Page) -> dict:
        return {"page": p, "children": [node(c) for c in children.get(p.id, [])]}

    return [node(p) for p in children.get(None, [])]


def _descendant_ids(page: Page) -> set[int]:
    by_parent: dict[int, list[Page]] = {}
    for p in page.space.pages:
        if p.parent_id:
            by_parent.setdefault(p.parent_id, []).append(p)
    out, stack = set(), [page.id]
    while stack:
        for c in by_parent.get(stack.pop(), []):
            out.add(c.id)
            stack.append(c.id)
    return out


def _parent_from_form(db: Session, space: Space, raw: str, page: Page | None = None) -> int | None:
    if not raw.isdigit():
        return None
    parent = db.get(Page, int(raw))
    if not parent or parent.space_id != space.id:
        raise HTTPException(400, "Родительская статья должна быть в том же пространстве")
    if page and (parent.id == page.id or parent.id in _descendant_ids(page)):
        raise HTTPException(400, "Нельзя сделать статью дочерней самой себе")
    return parent.id


def _title_body(form) -> tuple[str, str]:
    title = str(form.get("title", "")).strip()
    if not title or len(title) > 255:
        raise HTTPException(400, "Заголовок обязателен (до 255 символов)")
    return title, str(form.get("body", ""))[:200000]


def _snapshot(page: Page, user: User) -> PageVersion:
    return PageVersion(page_id=page.id, version=page.version, title=page.title, body=page.body, author_id=user.id)


def _issue_or_none(db: Session, user: User, key: str) -> Issue | None:
    if not key or not user.can("jira.view"):
        return None
    try:
        return jira.get_issue_by_key(db, key)
    except HTTPException:
        return None


def search_pages(db: Session, q: str, limit: int = 50) -> list[tuple[Page, str]]:
    """Поиск по заголовку и тексту (casefold — регистр кириллицы). Возвращает (статья, фрагмент)."""
    needle = q.casefold().strip()
    if not needle:
        return []
    results = []
    for p in db.scalars(select(Page).order_by(Page.updated_at.desc())):
        in_title = needle in p.title.casefold()
        idx = p.body.casefold().find(needle)
        if in_title or idx >= 0:
            snippet = p.body[max(0, idx - 60): idx + 120] if idx >= 0 else p.body[:160]
            results.append((p, snippet))
            if len(results) >= limit:
                break
    return results


# ---------- Страницы ----------

@router.get("/confluence")
def confluence_home(request: Request, user: User = Depends(require("confluence.view")),
                    db: Session = Depends(get_db)):
    spaces = db.scalars(select(Space).order_by(Space.name)).all()
    recent = db.scalars(select(Page).order_by(Page.updated_at.desc()).limit(10)).all()
    return render(request, "confluence/index.html", {"title": "Confluence", "spaces": spaces, "recent": recent},
                  user, db)


@router.get("/confluence/search")
def search(request: Request, q: str = "", user: User = Depends(require("confluence.view")),
           db: Session = Depends(get_db)):
    return render(request, "confluence/search.html",
                  {"title": "Поиск", "q": q, "results": search_pages(db, q[:200])}, user, db)


@router.get("/confluence/spaces/{key}")
def space_page(key: str, request: Request, user: User = Depends(require("confluence.view")),
               db: Session = Depends(get_db)):
    space = _space(db, key)
    return render(request, "confluence/space.html",
                  {"title": space.name, "space": space, "tree": build_tree(space.pages), "current": None}, user, db)


@router.get("/confluence/pages/{page_id}")
def page_view(page_id: int, request: Request, user: User = Depends(require("confluence.view")),
              db: Session = Depends(get_db)):
    page = _page(db, page_id)
    return render(request, "confluence/page.html", {
        "title": page.title, "page": page, "space": page.space, "tree": build_tree(page.space.pages),
        "current": page.id, "show_issues": user.can("jira.view"),
    }, user, db)


@router.get("/confluence/new")
def page_new(request: Request, space: str = "", parent: str = "", issue: str = "",
             user: User = Depends(require("confluence.edit")), db: Session = Depends(get_db)):
    spaces = db.scalars(select(Space).order_by(Space.name)).all()
    if not spaces:
        raise HTTPException(400, "Сначала создайте пространство")
    linked = _issue_or_none(db, user, issue)
    current_space = next((s for s in spaces if s.key == space.upper()), spaces[0])
    return render(request, "confluence/edit.html", {
        "title": "Новая статья", "page": None, "spaces": spaces, "space": current_space,
        "parent_id": int(parent) if parent.isdigit() else None, "issue": linked,
        "draft_title": f"{linked.key}: {linked.title}" if linked else "",
    }, user, db)


@router.get("/confluence/pages/{page_id}/edit")
def page_edit(page_id: int, request: Request, user: User = Depends(require("confluence.edit")),
              db: Session = Depends(get_db)):
    page = _page(db, page_id)
    return render(request, "confluence/edit.html", {
        "title": f"Редактирование: {page.title}", "page": page, "space": page.space,
        "spaces": [page.space], "parent_id": page.parent_id, "issue": None,
        "exclude": _descendant_ids(page) | {page.id},
    }, user, db)


@router.get("/confluence/pages/{page_id}/history")
def page_history(page_id: int, request: Request, v: int | None = None,
                 user: User = Depends(require("confluence.view")), db: Session = Depends(get_db)):
    page = _page(db, page_id)
    selected = next((x for x in page.versions if x.version == v), None) if v else None
    return render(request, "confluence/history.html", {
        "title": f"История: {page.title}", "page": page, "selected": selected}, user, db)


# ---------- Изменение ----------

@router.post("/confluence/spaces")
async def space_create(request: Request, user: User = Depends(require("confluence.edit")),
                       db: Session = Depends(get_db)):
    form = await request.form()
    key, name = str(form.get("key", "")).strip().upper(), str(form.get("name", "")).strip()[:128]
    if not SPACE_KEY_RE.match(key) or not name:
        raise HTTPException(400, "Ключ: 2–16 латинских букв/цифр, имя обязательно")
    if db.scalar(select(Space).where(Space.key == key)):
        raise HTTPException(400, "Пространство с таким ключом уже есть")
    db.add(Space(key=key, name=name, description=str(form.get("description", "")).strip()[:1000]))
    db.commit()
    return RedirectResponse(f"/confluence/spaces/{key}", 303)


@router.post("/confluence/pages")
async def page_create(request: Request, user: User = Depends(require("confluence.edit")),
                      db: Session = Depends(get_db)):
    form = await request.form()
    space = _space(db, str(form.get("space", "")))
    title, body = _title_body(form)
    page = Page(space_id=space.id, title=title, body=body, author_id=user.id,
                parent_id=_parent_from_form(db, space, str(form.get("parent_id", ""))))
    # «Создать артефакт»: статья сразу связывается с задачей
    issue = _issue_or_none(db, user, str(form.get("issue", "")))
    if issue:
        page.issues.append(issue)
    db.add(page)
    db.flush()
    db.add(_snapshot(page, user))
    db.commit()
    return RedirectResponse(f"/confluence/pages/{page.id}", 303)


@router.post("/confluence/pages/{page_id}")
async def page_update(page_id: int, request: Request, user: User = Depends(require("confluence.edit")),
                      db: Session = Depends(get_db)):
    page = _page(db, page_id)
    form = await request.form()
    title, body = _title_body(form)
    page.parent_id = _parent_from_form(db, page.space, str(form.get("parent_id", "")), page)
    if (title, body) != (page.title, page.body):
        page.title, page.body, page.version = title, body, page.version + 1
        page.author_id, page.updated_at = user.id, utcnow()
        db.add(_snapshot(page, user))
    db.commit()
    return RedirectResponse(f"/confluence/pages/{page.id}", 303)


@router.post("/confluence/pages/{page_id}/restore/{version}")
def page_restore(page_id: int, version: int, user: User = Depends(require("confluence.edit")),
                 db: Session = Depends(get_db)):
    page = _page(db, page_id)
    old = next((x for x in page.versions if x.version == version), None)
    if not old:
        raise HTTPException(404, "Версия не найдена")
    page.title, page.body, page.version = old.title, old.body, page.version + 1
    page.author_id, page.updated_at = user.id, utcnow()
    db.add(_snapshot(page, user))
    db.commit()
    return RedirectResponse(f"/confluence/pages/{page.id}", 303)


@router.post("/confluence/pages/{page_id}/delete")
def page_delete(page_id: int, user: User = Depends(require("confluence.edit")), db: Session = Depends(get_db)):
    page = _page(db, page_id)
    # Дочерние статьи поднимаются на уровень удаляемой, а не пропадают
    for child in db.scalars(select(Page).where(Page.parent_id == page.id)):
        child.parent_id = page.parent_id
    key = page.space.key
    db.delete(page)
    db.commit()
    return RedirectResponse(f"/confluence/spaces/{key}", 303)


# ---------- Связи задача ↔ статья ----------

@router.post("/confluence/pages/{page_id}/issues")
async def link_issue(page_id: int, request: Request, user: User = Depends(require("confluence.edit")),
                     db: Session = Depends(get_db)):
    page = _page(db, page_id)
    issue = _issue_or_none(db, user, str((await request.form()).get("issue", "")).strip())
    if not issue:
        raise HTTPException(400, "Задача не найдена")
    if issue not in page.issues:
        page.issues.append(issue)
        db.commit()
    return RedirectResponse(f"/confluence/pages/{page.id}", 303)


@router.post("/confluence/pages/{page_id}/issues/{issue_id}/delete")
def unlink_issue(page_id: int, issue_id: int, user: User = Depends(require("confluence.edit")),
                 db: Session = Depends(get_db)):
    page = _page(db, page_id)
    page.issues = [i for i in page.issues if i.id != issue_id]
    db.commit()
    return RedirectResponse(f"/confluence/pages/{page.id}", 303)


@router.post("/jira/issues/{key}/pages")
async def link_page_from_issue(key: str, request: Request, user: User = Depends(require("jira.edit")),
                               db: Session = Depends(get_db)):
    issue = jira.get_issue_by_key(db, key)
    raw = str((await request.form()).get("page_id", ""))
    page = db.get(Page, int(raw)) if raw.isdigit() else None
    if not page or not user.can("confluence.view"):
        raise HTTPException(400, "Статья не найдена")
    if page not in issue.pages:
        issue.pages.append(page)
        db.commit()
    return RedirectResponse(f"/jira/issues/{issue.key}", 303)


@router.post("/jira/issues/{key}/pages/{page_id}/delete")
def unlink_page_from_issue(key: str, page_id: int, user: User = Depends(require("jira.edit")),
                           db: Session = Depends(get_db)):
    issue = jira.get_issue_by_key(db, key)
    issue.pages = [p for p in issue.pages if p.id != page_id]
    db.commit()
    return RedirectResponse(f"/jira/issues/{issue.key}", 303)


def _issue_related(db: Session, user: User, issue: Issue) -> dict:
    """Блок «Связанные статьи» на странице задачи."""
    if not user.can("confluence.view"):
        return {}
    linked = {p.id for p in issue.pages}
    candidates = [p for p in db.scalars(select(Page).order_by(Page.title).limit(500)) if p.id not in linked]
    has_spaces = db.scalar(select(func.count()).select_from(Space)) > 0
    return {"pages": issue.pages, "candidates": candidates, "has_spaces": has_spaces}


jira.ISSUE_EXTRA = _issue_related


# ---------- Главная ----------

@widget("pages", "confluence.view")
def _pages_widget(db: Session, user: User) -> dict:
    return {"pages": db.scalars(select(Page).order_by(Page.updated_at.desc()).limit(6)).all()}


@kpi("confluence.view")
def _pages_kpi(db: Session, user: User) -> dict:
    return {"icon": "book-open", "label": "Статей", "value": db.scalar(select(func.count()).select_from(Page)),
            "link": "/confluence"}
