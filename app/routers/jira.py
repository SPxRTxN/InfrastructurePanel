import re
from collections import Counter
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Board, BoardColumn, Issue, User
from app.models.jira import DEFAULT_COLUMNS, PRIORITIES
from app.routers.home import kpi, widget
from app.web import render, require

router = APIRouter()

KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


# ---------- Вспомогательное ----------

def _board(db: Session, board_id: int) -> Board:
    board = db.get(Board, board_id)
    if not board:
        raise HTTPException(404, "Доска не найдена")
    return board


def get_issue_by_key(db: Session, key: str) -> Issue:
    prefix, _, number = key.upper().rpartition("-")
    issue = None
    if number.isdigit():
        issue = db.scalar(select(Issue).join(Board).where(Board.key == prefix, Issue.number == int(number)))
    if not issue:
        raise HTTPException(404, "Задача не найдена")
    return issue


def _normalize_labels(raw: str) -> str:
    labels = [x.strip().lower()[:32] for x in raw.replace(";", ",").split(",")]
    return ",".join(dict.fromkeys(x for x in labels if x))[:500]


def _apply_issue_form(db: Session, issue: Issue, form) -> None:
    title = str(form.get("title", "")).strip()
    if not title or len(title) > 255:
        raise HTTPException(400, "Заголовок обязателен (до 255 символов)")
    priority = form.get("priority", "medium")
    if priority not in PRIORITIES:
        raise HTTPException(400, "Неверный приоритет")
    due = str(form.get("due_date", "")).strip()
    try:
        issue.due_date = date.fromisoformat(due) if due else None
    except ValueError:
        raise HTTPException(400, "Неверная дата") from None
    assignee = str(form.get("assignee_id", "")).strip()
    issue.assignee_id = int(assignee) if assignee.isdigit() and db.get(User, int(assignee)) else None
    issue.title, issue.priority = title, priority
    issue.description = str(form.get("description", ""))[:50000]
    issue.labels = _normalize_labels(str(form.get("labels", "")))


def _matches(issue: Issue, q: str, assignee: str, priority: str, label: str) -> bool:
    # Фильтр в Python: SQLite lower() не понимает кириллицу, а задач в домашней лаборатории немного
    if q:
        hay = f"{issue.key} {issue.title} {issue.description} {issue.labels}".casefold()
        if q.casefold() not in hay:
            return False
    if assignee == "none" and issue.assignee_id is not None:
        return False
    if assignee.isdigit() and issue.assignee_id != int(assignee):
        return False
    if priority and issue.priority != priority:
        return False
    return not label or label in issue.label_list


def _users(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.username)))


# ---------- Страницы ----------

@router.get("/jira")
def jira_root(request: Request, user: User = Depends(require("jira.view")), db: Session = Depends(get_db)):
    first = db.scalar(select(Board).order_by(Board.id).limit(1))
    if first:
        return RedirectResponse(f"/jira/boards/{first.id}", 303)
    return render(request, "jira/board.html", {"title": "Jira", "board": None, "boards": []}, user, db)


@router.get("/jira/boards/{board_id}")
def board_page(board_id: int, request: Request, q: str = "", assignee: str = "", priority: str = "",
               label: str = "", user: User = Depends(require("jira.view")), db: Session = Depends(get_db)):
    board = _board(db, board_id)
    columns = [(c, [i for i in c.issues if _matches(i, q, assignee, priority, label)]) for c in board.columns]
    all_labels = sorted({lbl for i in board.issues for lbl in i.label_list})
    return render(request, "jira/board.html", {
        "title": "Jira", "board": board, "boards": db.scalars(select(Board).order_by(Board.name)).all(),
        "columns": columns, "users": _users(db), "priorities": PRIORITIES, "all_labels": all_labels,
        "f": {"q": q, "assignee": assignee, "priority": priority, "label": label},
        "today": date.today(),
    }, user, db)


@router.get("/jira/issues/{key}")
def issue_page(key: str, request: Request, user: User = Depends(require("jira.view")),
               db: Session = Depends(get_db)):
    issue = get_issue_by_key(db, key)
    return render(request, "jira/issue.html", {
        "title": issue.key, "issue": issue, "users": _users(db), "priorities": PRIORITIES,
        "related": ISSUE_EXTRA(db, user, issue) if ISSUE_EXTRA else {},
    }, user, db)


# Хук для связанных статей Confluence (заполняется модулем confluence)
ISSUE_EXTRA = None


# ---------- Доски и колонки ----------

@router.post("/jira/boards")
async def board_create(request: Request, user: User = Depends(require("jira.edit")), db: Session = Depends(get_db)):
    form = await request.form()
    key, name = str(form.get("key", "")).strip().upper(), str(form.get("name", "")).strip()[:128]
    if not KEY_RE.match(key) or not name:
        raise HTTPException(400, "Ключ: 2–10 латинских букв/цифр, начинается с буквы; имя обязательно")
    if db.scalar(select(Board).where(Board.key == key)):
        raise HTTPException(400, "Доска с таким ключом уже есть")
    board = Board(key=key, name=name, columns=[BoardColumn(name=n, position=i) for i, n in enumerate(DEFAULT_COLUMNS)])
    db.add(board)
    db.commit()
    return RedirectResponse(f"/jira/boards/{board.id}", 303)


@router.post("/jira/boards/{board_id}/columns")
async def column_create(board_id: int, request: Request, user: User = Depends(require("jira.edit")),
                        db: Session = Depends(get_db)):
    board = _board(db, board_id)
    name = str((await request.form()).get("name", "")).strip()[:64]
    if not name:
        raise HTTPException(400, "Имя колонки обязательно")
    # Новая колонка встаёт перед последней («Готово» остаётся последней)
    pos = len(board.columns) - 1 if board.columns else 0
    for c in board.columns[pos:]:
        c.position += 1
    db.add(BoardColumn(board_id=board.id, name=name, position=pos))
    db.commit()
    return RedirectResponse(f"/jira/boards/{board.id}", 303)


def _column(db: Session, column_id: int) -> BoardColumn:
    col = db.get(BoardColumn, column_id)
    if not col:
        raise HTTPException(404, "Колонка не найдена")
    return col


@router.post("/jira/columns/{column_id}")
async def column_update(column_id: int, request: Request, user: User = Depends(require("jira.edit")),
                        db: Session = Depends(get_db)):
    col = _column(db, column_id)
    form = await request.form()
    name = str(form.get("name", col.name)).strip()[:64]
    if name:
        col.name = name
    move = form.get("move")
    if move in ("left", "right"):
        cols = list(col.board.columns)
        i = cols.index(col)
        j = i - 1 if move == "left" else i + 1
        if 0 <= j < len(cols):
            cols[i].position, cols[j].position = cols[j].position, cols[i].position
    db.commit()
    return RedirectResponse(f"/jira/boards/{col.board_id}", 303)


@router.post("/jira/columns/{column_id}/delete")
def column_delete(column_id: int, user: User = Depends(require("jira.edit")), db: Session = Depends(get_db)):
    col = _column(db, column_id)
    if col.issues:
        raise HTTPException(400, "В колонке есть задачи — сначала перенесите их")
    if len(col.board.columns) <= 1:
        raise HTTPException(400, "Должна остаться хотя бы одна колонка")
    board_id = col.board_id
    db.delete(col)
    db.commit()
    return RedirectResponse(f"/jira/boards/{board_id}", 303)


# ---------- Задачи ----------

@router.post("/jira/boards/{board_id}/issues")
async def issue_create(board_id: int, request: Request, user: User = Depends(require("jira.edit")),
                       db: Session = Depends(get_db)):
    board = _board(db, board_id)
    form = await request.form()
    column_id = str(form.get("column_id", ""))
    column = next((c for c in board.columns if str(c.id) == column_id), board.columns[0] if board.columns else None)
    if column is None:
        raise HTTPException(400, "На доске нет колонок")
    issue = Issue(board_id=board.id, column_id=column.id, number=board.next_number,
                  position=len(column.issues))
    _apply_issue_form(db, issue, form)
    board.next_number += 1
    db.add(issue)
    db.commit()
    return RedirectResponse(f"/jira/boards/{board.id}", 303)


@router.post("/jira/issues/{key}")
async def issue_update(key: str, request: Request, user: User = Depends(require("jira.edit")),
                       db: Session = Depends(get_db)):
    issue = get_issue_by_key(db, key)
    form = await request.form()
    _apply_issue_form(db, issue, form)
    column_id = str(form.get("column_id", ""))
    col = next((c for c in issue.board.columns if str(c.id) == column_id), None)
    if col and col.id != issue.column_id:
        issue.column_id, issue.position = col.id, len(col.issues)
    db.commit()
    return RedirectResponse(f"/jira/issues/{issue.key}", 303)


@router.post("/jira/issues/{key}/delete")
def issue_delete(key: str, user: User = Depends(require("jira.edit")), db: Session = Depends(get_db)):
    issue = get_issue_by_key(db, key)
    board_id = issue.board_id
    db.delete(issue)
    db.commit()
    return RedirectResponse(f"/jira/boards/{board_id}", 303)


class MoveIn(BaseModel):
    column_id: int
    order: list[int]  # id задач в целевой колонке в новом порядке


@router.post("/api/jira/issues/{issue_id}/move")
def issue_move(issue_id: int, body: MoveIn, user: User = Depends(require("jira.edit")),
               db: Session = Depends(get_db)):
    """Перетаскивание карточки: колонка и порядок задач в ней."""
    issue = db.get(Issue, issue_id)
    if not issue:
        raise HTTPException(404, "Задача не найдена")
    col = db.get(BoardColumn, body.column_id)
    if not col or col.board_id != issue.board_id:
        raise HTTPException(400, "Колонка с другой доски")
    issue.column_id = col.id
    by_id = {i.id: i for i in db.scalars(select(Issue).where(Issue.board_id == issue.board_id,
                                                              Issue.id.in_(body.order)))}
    for pos, iid in enumerate(body.order):
        if iid in by_id:
            by_id[iid].position = pos
    db.commit()
    return {"ok": True, "key": issue.key, "column": col.name}


# ---------- Главная ----------

@widget("tasks", "jira.view")
def _tasks_widget(db: Session, user: User) -> dict:
    counts: Counter[str] = Counter()
    order: list[str] = []
    for col in db.scalars(select(BoardColumn).order_by(BoardColumn.position)):
        if col.name not in order:
            order.append(col.name)
        counts[col.name] += len(col.issues)
    upcoming = db.scalars(select(Issue).where(Issue.due_date.is_not(None)).order_by(Issue.due_date)).all()
    upcoming = [i for i in upcoming if not i.is_done][:6]
    return {"labels": order, "values": [counts[n] for n in order], "upcoming": upcoming, "today": date.today()}


@kpi("jira.view")
def _tasks_kpi(db: Session, user: User) -> dict:
    total = db.scalar(select(func.count()).select_from(Issue))
    done = sum(1 for i in db.scalars(select(Issue)) if i.is_done)
    return {"icon": "square-kanban", "label": "Открытых задач", "value": total - done, "link": "/jira"}
