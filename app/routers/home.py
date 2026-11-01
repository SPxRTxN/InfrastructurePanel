"""Главная: набор и порядок виджетов из настроек пользователя.

Модули регистрируют данные виджетов в WIDGETS и KPI-карточки в KPI.
Шаблон виджета — templates/home/<id>.html.
"""
from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.services.settings_registry import HOME_WIDGETS, get_setting
from app.web import current_user, render

router = APIRouter()

# id виджета -> (право или None, функция (db, user) -> контекст шаблона)
WIDGETS: dict[str, tuple[str | None, Callable[[Session, User], dict]]] = {}
# (право, функция (db, user) -> {"icon", "label", "value", "link"})
KPI: list[tuple[str, Callable[[Session, User], dict]]] = []


def widget(name: str, perm: str | None = None):
    def deco(fn):
        WIDGETS[name] = (perm, fn)
        return fn

    return deco


def kpi(perm: str):
    def deco(fn):
        KPI.append((perm, fn))
        return fn

    return deco


@widget("kpi")
def _kpi_widget(db: Session, user: User) -> dict:
    return {"kpis": [fn(db, user) for perm, fn in KPI if user.can(perm)]}


@router.get("/")
def home(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    widgets = []
    for name in get_setting(db, "home.widgets", user.id):
        if name not in HOME_WIDGETS or name not in WIDGETS:
            continue
        perm, fn = WIDGETS[name]
        if perm is None or user.can(perm):
            widgets.append((name, fn(db, user)))
    return render(request, "home.html", {"title": "Главная", "widgets": widgets}, user, db)
