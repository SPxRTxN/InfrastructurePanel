from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.models import Board, Event, Notification, User
from app.services import calendar as cal


def _ev(**kw):
    base = {"title": "E", "start": datetime(2026, 1, 31, 10, 0), "end": datetime(2026, 1, 31, 11, 0),
            "repeat": "none", "all_day": False}
    return Event(**{**base, **kw})


def test_occurrences_none_daily_weekly():
    e = _ev()
    assert cal.occurrences(e, datetime(2026, 1, 31), datetime(2026, 2, 1)) == [datetime(2026, 1, 31, 10)]
    assert cal.occurrences(e, datetime(2026, 2, 1), datetime(2026, 2, 2)) == []
    d = _ev(repeat="daily")
    assert len(cal.occurrences(d, datetime(2026, 3, 1), datetime(2026, 3, 8))) == 7
    w = _ev(repeat="weekly")
    occ = cal.occurrences(w, datetime(2026, 2, 1), datetime(2026, 3, 1))
    assert occ == [datetime(2026, 2, 7, 10), datetime(2026, 2, 14, 10), datetime(2026, 2, 21, 10), datetime(2026, 2, 28, 10)]
    assert cal.occurrences(w, datetime(2025, 1, 1), datetime(2025, 12, 31)) == []  # до начала серии


def test_occurrences_monthly_skips_short_months():
    m = _ev(repeat="monthly")
    occ = cal.occurrences(m, datetime(2026, 1, 1), datetime(2026, 6, 1))
    assert occ == [datetime(2026, 1, 31, 10), datetime(2026, 3, 31, 10), datetime(2026, 5, 31, 10)]


def test_reminders_sent_once(db):
    now = datetime(2026, 5, 10, 9, 50)
    db.add(_ev(title="Бэкап", start=datetime(2026, 5, 10, 10, 0), end=datetime(2026, 5, 10, 11), remind_minutes=15))
    db.add(_ev(title="Позже", start=datetime(2026, 5, 10, 12, 0), end=None, remind_minutes=15))
    db.add(_ev(title="Без напоминания", start=datetime(2026, 5, 10, 10, 0), remind_minutes=None))
    db.commit()
    assert cal.send_reminders(db, now) == 1
    assert cal.send_reminders(db, now) == 0  # повторно не шлём
    notes = db.scalars(select(Notification)).all()
    # все три тестовых пользователя имеют calendar.view
    assert len(notes) == 3 and notes[0].title == "Напоминание: Бэкап"


def test_recurring_reminder_each_occurrence(db):
    db.add(_ev(title="Daily", start=datetime(2026, 5, 1, 8, 0), repeat="daily", remind_minutes=5))
    db.commit()
    assert cal.send_reminders(db, datetime(2026, 5, 3, 7, 56)) == 1
    assert cal.send_reminders(db, datetime(2026, 5, 4, 7, 56)) == 1
    # слишком поздно (сервис был выключен) — старое не шлём
    assert cal.send_reminders(db, datetime(2026, 5, 5, 9, 0)) == 0


def test_reminders_skip_users_without_permission(db):
    from app.models import Group
    g = db.scalar(select(Group).where(Group.name == "Наблюдатели"))
    g.permissions = [p for p in g.permissions if p.perm != "calendar.view"]
    db.add(_ev(start=datetime(2026, 5, 10, 10, 0), remind_minutes=10))
    db.commit()
    cal.send_reminders(db, datetime(2026, 5, 10, 9, 55))
    viewer = db.scalar(select(User).where(User.username == "viewer"))
    assert not db.scalar(select(Notification).where(Notification.user_id == viewer.id))


def test_notifications_api(admin, db):
    db.add(_ev(start=datetime(2026, 5, 10, 10, 0), remind_minutes=10))
    db.commit()
    cal.send_reminders(db, datetime(2026, 5, 10, 9, 55))
    data = admin.get("/api/notifications").json()
    assert data["unread"] == 1 and data["items"][0]["link"] == "/calendar"
    admin.post("/api/notifications/read-all")
    assert admin.get("/api/notifications").json()["unread"] == 0


def test_create_event_and_api(admin, db):
    r = admin.post("/calendar/events", data={"title": "Обновление", "start_date": "2026-11-03", "start_time": "20:00",
                                             "end_date": "2026-11-03", "end_time": "21:30", "repeat": "weekly",
                                             "remind_minutes": "30"}, follow_redirects=False)
    assert r.status_code == 303
    e = db.scalar(select(Event))
    assert e.start == datetime(2026, 11, 3, 20) and e.end == datetime(2026, 11, 3, 21, 30) and e.remind_minutes == 30
    items = admin.get("/api/calendar/events", params={"start": "2026-11-01T00:00:00+00:00",
                                                      "end": "2026-12-01T00:00:00+00:00"}).json()
    assert len([i for i in items if i["title"] == "Обновление"]) == 4


def test_all_day_event(admin, db):
    admin.post("/calendar/events", data={"title": "Отпуск", "all_day": "on", "start_date": "2026-11-10",
                                         "end_date": "2026-11-12", "repeat": "none"})
    e = db.scalar(select(Event))
    assert e.all_day and e.start == datetime(2026, 11, 10) and e.end == datetime(2026, 11, 13)
    item = admin.get("/api/calendar/events", params={"start": "2026-11-01", "end": "2026-12-01"}).json()[0]
    assert item["start"] == "2026-11-10" and item["end"] == "2026-11-13" and item["allDay"]


def test_event_validation(admin):
    base = {"title": "x", "start_date": "2026-11-03", "start_time": "10:00", "end_date": "2026-11-03"}
    assert admin.post("/calendar/events", data={**base, "end_time": "09:00"}).status_code == 400
    assert admin.post("/calendar/events", data={**base, "title": ""}).status_code == 400
    assert admin.post("/calendar/events", data={**base, "repeat": "yearly"}).status_code == 400
    assert admin.post("/calendar/events", data={**base, "start_date": "bad"}).status_code == 400
    assert admin.post("/calendar/events", data={**base, "remind_minutes": "-5"}).status_code == 400


def test_update_delete_and_permissions(admin, viewer, db):
    admin.post("/calendar/events", data={"title": "A", "start_date": "2026-11-03", "start_time": "10:00"})
    e = db.scalar(select(Event))
    assert viewer.post(f"/calendar/events/{e.id}", data={"title": "B", "start_date": "2026-11-03"}).status_code == 403
    assert viewer.get("/calendar").status_code == 200
    assert "event-form" not in viewer.get("/calendar").text
    admin.post(f"/calendar/events/{e.id}", data={"title": "B", "start_date": "2026-11-04", "start_time": "10:00"})
    db.refresh(e)
    assert e.title == "B" and e.end == datetime(2026, 11, 4, 11)
    admin.post(f"/calendar/events/{e.id}/delete")
    assert db.scalar(select(Event)) is None


def test_jira_due_dates_in_calendar(admin, viewer, db):
    admin.post("/jira/boards", data={"key": "HL", "name": "H"})
    board = db.scalar(select(Board))
    admin.post(f"/jira/boards/{board.id}/issues", data={"title": "Сдать отчёт", "priority": "low",
                                                        "due_date": "2026-11-20"})
    params = {"start": "2026-11-01T00:00:00+03:00", "end": "2026-12-01T00:00:00+03:00"}
    items = admin.get("/api/calendar/events", params=params).json()
    assert items[0]["title"] == "HL-1: Сдать отчёт" and items[0]["url"] == "/jira/issues/HL-1"
    from app.models import Group
    g = db.scalar(select(Group).where(Group.name == "Наблюдатели"))
    g.permissions = [p for p in g.permissions if p.perm != "jira.view"]
    db.commit()
    assert viewer.get("/api/calendar/events", params=params).json() == []


def test_calendar_range_limit(admin):
    assert admin.get("/api/calendar/events", params={"start": "2020-01-01", "end": "2026-01-01"}).status_code == 400


def test_home_events_widget(admin, db):
    tomorrow = datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).replace(hour=12)
    db.add(_ev(title="Замена диска", start=tomorrow, end=tomorrow + timedelta(hours=1)))
    db.commit()
    html = admin.get("/").text
    assert "Замена диска" in html and "Событий сегодня" in html
