"""Повторяющиеся события и напоминания."""
import calendar as pycal
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event, Notification, ReminderLog, User

# Насколько поздно ещё отправлять напоминание (например, после перезапуска сервиса)
LATE_GRACE = timedelta(minutes=10)


def now_local() -> datetime:
    return datetime.now().replace(microsecond=0)


def duration(event: Event) -> timedelta:
    if event.end and event.end > event.start:
        return event.end - event.start
    return timedelta(days=1) if event.all_day else timedelta(hours=1)


def _add_months(dt: datetime, months: int) -> datetime | None:
    """Тот же день месяца через N месяцев; None, если такого дня нет (31-е в апреле)."""
    total = dt.month - 1 + months
    year, month = dt.year + total // 12, total % 12 + 1
    if dt.day > pycal.monthrange(year, month)[1]:
        return None
    return dt.replace(year=year, month=month)


def occurrences(event: Event, start: datetime, end: datetime) -> list[datetime]:
    """Начала вхождений события, пересекающих интервал [start, end)."""
    dur = duration(event)
    if event.repeat == "none":
        return [event.start] if event.start < end and event.start + dur > start else []

    out: list[datetime] = []
    if event.repeat in ("daily", "weekly"):
        step = timedelta(days=1 if event.repeat == "daily" else 7)
        # Перепрыгиваем сразу к первому вхождению около начала интервала
        skip = max(0, (start - dur - event.start) // step)
        cur = event.start + step * skip
        while cur < end:
            if cur + dur > start:
                out.append(cur)
            cur += step
        return out

    # monthly
    skip = max(0, (start.year - event.start.year) * 12 + start.month - event.start.month - 1)
    for i in range(skip, skip + 1200):
        cur = _add_months(event.start, i)
        if cur is None:
            continue
        if cur >= end:
            break
        if cur + dur > start:
            out.append(cur)
    return out


def upcoming(db: Session, days: int = 7, limit: int = 6) -> list[tuple[Event, datetime]]:
    now = now_local()
    items = [(e, occ) for e in db.scalars(select(Event)) for occ in occurrences(e, now, now + timedelta(days=days))]
    return sorted(items, key=lambda x: x[1])[:limit]


def due_reminders(db: Session, now: datetime) -> list[tuple[Event, datetime]]:
    """Вхождения, для которых пора отправить напоминание и оно ещё не отправлено."""
    result = []
    for event in db.scalars(select(Event).where(Event.remind_minutes.is_not(None))):
        lead = timedelta(minutes=event.remind_minutes)
        for occ in occurrences(event, now - LATE_GRACE, now + lead + timedelta(seconds=1)):
            if occ - lead <= now and occ >= now - LATE_GRACE:
                sent = db.scalar(select(ReminderLog).where(ReminderLog.event_id == event.id,
                                                           ReminderLog.occurrence == occ))
                if not sent:
                    result.append((event, occ))
    return result


def send_reminders(db: Session, now: datetime | None = None) -> int:
    """Создаёт уведомления всем активным пользователям с calendar.view. Возвращает число напоминаний."""
    now = now or now_local()
    due = due_reminders(db, now)
    if not due:
        return 0
    users = [u for u in db.scalars(select(User).where(User.is_active.is_(True))) if u.can("calendar.view")]
    for event, occ in due:
        when = occ.strftime("%d.%m.%Y") if event.all_day else occ.strftime("%d.%m.%Y %H:%M")
        for u in users:
            db.add(Notification(user_id=u.id, title=f"Напоминание: {event.title}", body=f"Начало: {when}",
                                link="/calendar"))
        db.add(ReminderLog(event_id=event.id, occurrence=occ))
    db.commit()
    return len(due)
