from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Event, Issue, User
from app.models.calendar import REPEATS
from app.routers.home import kpi, widget
from app.services import calendar as cal
from app.services.settings_registry import get_setting
from app.web import render, require

router = APIRouter()


def _parse_range(value: str) -> datetime:
    """FullCalendar присылает ISO с часовым поясом браузера → локальное время сервера без tz."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "Неверная дата") from None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def _event_from_form(event: Event, form) -> None:
    title = str(form.get("title", "")).strip()
    if not title or len(title) > 255:
        raise HTTPException(400, "Название обязательно (до 255 символов)")
    repeat = form.get("repeat", "none")
    if repeat not in REPEATS:
        raise HTTPException(400, "Неверный повтор")
    all_day = form.get("all_day") == "on"
    try:
        start_d = date.fromisoformat(str(form.get("start_date", "")))
        end_raw = str(form.get("end_date", "")).strip()
        end_d = date.fromisoformat(end_raw) if end_raw else start_d
        start_t = time.fromisoformat(str(form.get("start_time") or "00:00")) if not all_day else time()
        end_t = time.fromisoformat(str(form.get("end_time") or "00:00")) if not all_day else time()
    except ValueError:
        raise HTTPException(400, "Неверная дата или время") from None
    start = datetime.combine(start_d, start_t)
    if all_day:
        end = datetime.combine(end_d + timedelta(days=1), time())  # конец не включается, как в FullCalendar
    elif form.get("end_time"):
        end = datetime.combine(end_d, end_t)
    else:
        end = start + timedelta(hours=1)
    if end <= start:
        raise HTTPException(400, "Конец должен быть позже начала")
    remind = str(form.get("remind_minutes", "")).strip()
    if remind and (not remind.isdigit() or int(remind) > 10080):
        raise HTTPException(400, "Напоминание: от 0 до 10080 минут")
    event.title, event.description = title, str(form.get("description", ""))[:5000]
    event.start, event.end, event.all_day, event.repeat = start, end, all_day, repeat
    event.remind_minutes = int(remind) if remind else None


def _event(db: Session, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Событие не найдено")
    return event


@router.get("/calendar")
def calendar_page(request: Request, user: User = Depends(require("calendar.view")), db: Session = Depends(get_db)):
    return render(request, "calendar.html", {
        "title": "Календарь", "repeats": REPEATS,
        "default_reminder": get_setting(db, "calendar.default_reminder", user.id),
        "first_day": get_setting(db, "calendar.first_day", user.id),
    }, user, db)


@router.get("/api/calendar/events")
def api_events(start: str, end: str, user: User = Depends(require("calendar.view")), db: Session = Depends(get_db)):
    """Источник событий для FullCalendar: вхождения событий + сроки задач Jira."""
    range_start, range_end = _parse_range(start), _parse_range(end)
    if range_end - range_start > timedelta(days=400):
        raise HTTPException(400, "Слишком большой диапазон")
    out = []
    for e in db.scalars(select(Event)):
        dur = cal.duration(e)
        for occ in cal.occurrences(e, range_start, range_end):
            fmt = "%Y-%m-%d" if e.all_day else "%Y-%m-%dT%H:%M:%S"
            out.append({
                "id": f"e{e.id}-{occ:%Y%m%d%H%M}", "title": e.title, "allDay": e.all_day,
                "start": occ.strftime(fmt), "end": (occ + dur).strftime(fmt),
                "extendedProps": {
                    "eventId": e.id, "description": e.description, "repeat": e.repeat,
                    "remind": e.remind_minutes, "seriesStart": e.start.isoformat(), "seriesEnd": e.end.isoformat() if e.end else None,
                },
            })
    if user.can("jira.view"):
        issues = db.scalars(select(Issue).where(Issue.due_date >= range_start.date(),
                                                Issue.due_date <= range_end.date())).all()
        for i in issues:
            out.append({
                "id": f"i{i.id}", "title": f"{i.key}: {i.title}", "start": i.due_date.isoformat(), "allDay": True,
                "url": f"/jira/issues/{i.key}", "classNames": ["fc-task", "fc-task-done" if i.is_done else ""],
                "extendedProps": {"issue": True},
            })
    return out


@router.post("/calendar/events")
async def event_create(request: Request, user: User = Depends(require("calendar.edit")),
                       db: Session = Depends(get_db)):
    event = Event(created_by=user.id)
    _event_from_form(event, await request.form())
    db.add(event)
    db.commit()
    return RedirectResponse("/calendar", 303)


@router.post("/calendar/events/{event_id}")
async def event_update(event_id: int, request: Request, user: User = Depends(require("calendar.edit")),
                       db: Session = Depends(get_db)):
    event = _event(db, event_id)
    _event_from_form(event, await request.form())
    db.commit()
    return RedirectResponse("/calendar", 303)


@router.post("/calendar/events/{event_id}/delete")
def event_delete(event_id: int, user: User = Depends(require("calendar.edit")), db: Session = Depends(get_db)):
    db.delete(_event(db, event_id))
    db.commit()
    return RedirectResponse("/calendar", 303)


# ---------- Главная ----------

@widget("events", "calendar.view")
def _events_widget(db: Session, user: User) -> dict:
    return {"events": cal.upcoming(db, days=14)}


@kpi("calendar.view")
def _events_kpi(db: Session, user: User) -> dict:
    today = datetime.combine(date.today(), time())
    count = sum(len(cal.occurrences(e, today, today + timedelta(days=1))) for e in db.scalars(select(Event)))
    return {"icon": "calendar", "label": "Событий сегодня", "value": count, "link": "/calendar"}
