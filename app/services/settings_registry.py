"""Реестр настроек. Новая настройка = одна запись в SETTINGS.

Значение ищется так: пользовательское (для scope=user) → глобальное → default.
"""
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import config
from app.models import SettingValue

SECTIONS = {
    "appearance": "Внешний вид",
    "home": "Главная",
    "calendar": "Календарь",
    "intervals": "Интервалы и хранение",
    "modules": "Разделы",
    "security": "Безопасность",
}

# Разделы, которые можно выключить (префикс права → название). Выключенный раздел пропадает из меню,
# виджетов и API у всех: так одна и та же панель годится и для основного сервера, и для Pi / узла администрирования.
MODULES = [
    ("servers", "Серверы и узлы"),
    ("topology", "Топология"),
    ("jira", "Jira"),
    ("confluence", "Confluence"),
    ("calendar", "Календарь"),
    ("services", "Сервисы"),
]

ACCENTS = [
    ("indigo", "Индиго"),
    ("pink", "Розовый"),
    ("violet", "Фиолетовый"),
    ("blue", "Синий"),
    ("cyan", "Бирюзовый"),
    ("green", "Зелёный"),
    ("orange", "Оранжевый"),
]

HOME_WIDGETS = {
    "kpi": "KPI-карточки",
    "servers": "Статус серверов",
    "tasks": "Задачи по статусам",
    "events": "Ближайшие события",
    "pages": "Последние статьи",
}


@dataclass
class SettingDef:
    key: str
    type: str  # str | int | bool | choice | list
    default: Any
    section: str
    scope: str  # global | user
    label: str
    choices: list[tuple[str, str]] = field(default_factory=list)
    min: int | None = None
    max: int | None = None


SETTINGS: list[SettingDef] = [
    SettingDef("appearance.theme", "choice", "dark", "appearance", "user", "Тема",
               [("dark", "Тёмная"), ("light", "Светлая")]),
    SettingDef("appearance.accent", "choice", "indigo", "appearance", "user", "Акцентный цвет", ACCENTS),
    SettingDef("appearance.density", "choice", "comfortable", "appearance", "user", "Плотность",
               [("comfortable", "Обычная"), ("compact", "Компактная")]),
    SettingDef("appearance.sidebar_collapsed", "bool", False, "appearance", "user", "Свёрнутый сайдбар"),
    SettingDef("home.widgets", "list", list(HOME_WIDGETS), "home", "user", "Виджеты и порядок",
               list(HOME_WIDGETS.items())),
    SettingDef("calendar.default_reminder", "int", 15, "calendar", "user",
               "Напоминание по умолчанию, мин", min=0, max=10080),
    SettingDef("calendar.first_day", "choice", "1", "calendar", "user", "Первый день недели",
               [("1", "Понедельник"), ("0", "Воскресенье")]),
    SettingDef("servers.check_interval", "int", 60, "intervals", "global",
               "Интервал проверки серверов, с", min=10, max=86400),
    SettingDef("agents.interval", "int", 10, "intervals", "global",
               "Интервал отправки данных агентами, с", min=2, max=3600),
    SettingDef("agents.history_every", "int", 60, "intervals", "global",
               "Шаг истории узлов, с (24 ч хранения)", min=10, max=3600),
    SettingDef("history.retention_days", "int", 14, "intervals", "global",
               "Хранение истории серверов, узлов и архива удалённого, дней", min=1, max=90),
    SettingDef("security.idle_minutes", "int", 30, "security", "global",
               "Выход после бездействия, мин (0 — не выходить; в режиме обновления администратора не выкидывает)",
               min=0, max=1440),
    SettingDef("console.idle_minutes", "int", 15, "security", "global",
               "SSH-консоль: закрыть после бездействия, мин", min=1, max=480),
    SettingDef("console.record", "choice", "always", "security", "global",
               "SSH-консоль: запись сеансов (только вывод, без ввода)",
               [("always", "всегда"), ("optional", "по галочке на странице консоли"), ("off", "не записывать")]),
    SettingDef("console.record_max_mb", "int", 20, "security", "global",
               "SSH-консоль: максимум записи одного сеанса, МБ (дальше запись обрезается)", min=1, max=200),
    SettingDef("console.record_days", "int", 30, "security", "global",
               "SSH-консоль: хранение записей, дней (закреплённые не удаляются)", min=1, max=3650),
    SettingDef("console.max_upload_mb", "int", 8192, "security", "global",
               "Передача файлов на сервер: наибольший размер, МБ (не больше 8 ГБ)", min=1, max=8192),
    SettingDef("security.log_days", "int", 90, "security", "global",
               "Хранение журнала входов и событий безопасности, дней", min=7, max=365),
    # Оповещения: своя страница (Инфраструктура → Оповещения), в общих разделах настроек не показываются
    SettingDef("alerts.silence_until", "str", "", "alerts", "global", "Тишина до (UTC, ISO)"),
    SettingDef("alerts.telegram_token", "str", "", "alerts", "global", "Токен бота Telegram (зашифрован)"),
    SettingDef("alerts.telegram_chat", "str", "", "alerts", "global", "Chat ID Telegram"),
    SettingDef("alerts.webhook_url", "str", "", "alerts", "global", "Webhook (ntfy/Gotify/свой)"),
    SettingDef("alerts.webhook_kind", "choice", "ntfy", "alerts", "global", "Формат webhook",
               [("ntfy", "ntfy (текст + заголовки)"), ("json", "JSON")]),
    # Бэкапы: своя страница (Настройки → Бэкапы). Включены сами, только если задан BACKUP_DIR
    SettingDef("backups.enabled", "bool", bool(config.backup_dir), "backups", "global", "Бэкап по расписанию"),
    SettingDef("backups.hour", "int", 4, "backups", "global", "Час запуска (0–23, время сервера)", min=0, max=23),
    SettingDef("backups.keep", "int", 14, "backups", "global", "Хранить копий", min=1, max=365),
    SettingDef("update.github_repo", "str", config.github_repo, "update", "global",
               "Репозиторий релизов панели (владелец/имя на GitHub)"),
    SettingDef("modules.enabled", "list", [m for m, _ in MODULES], "modules", "global",
               "Включённые разделы", MODULES),
]

REGISTRY = {s.key: s for s in SETTINGS}


def coerce(sdef: SettingDef, raw: Any) -> Any:
    """Приводит значение из формы к типу настройки. Неверное значение → ValueError."""
    if sdef.type == "bool":
        return raw in (True, "on", "true", "1", 1)
    if sdef.type == "int":
        try:
            val = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{sdef.label}: нужно целое число") from None
        if (sdef.min is not None and val < sdef.min) or (sdef.max is not None and val > sdef.max):
            raise ValueError(f"{sdef.label}: допустимо {sdef.min}–{sdef.max}")
        return val
    if sdef.type == "choice":
        if raw not in {c for c, _ in sdef.choices}:
            raise ValueError(f"{sdef.label}: недопустимое значение")
        return raw
    if sdef.type == "list":
        allowed = {c for c, _ in sdef.choices}
        items = [x for x in raw if x in allowed]
        return list(dict.fromkeys(items))  # без дублей, порядок сохраняется
    return str(raw)


def _row(db: Session, key: str, user_id: int | None) -> SettingValue | None:
    cond = SettingValue.user_id.is_(None) if user_id is None else SettingValue.user_id == user_id
    return db.scalar(select(SettingValue).where(SettingValue.key == key, cond))


def sanitize(sdef: SettingDef, value: Any) -> tuple[Any, bool]:
    """Сохранённое значение, приведённое к нынешнему реестру: (значение, годно_ли_как_есть).
    Пропавший вариант выбора, число вне границ, чужой тип → default; из списка выпадают пропавшие пункты."""
    t = sdef.type
    if t == "bool":
        ok = isinstance(value, bool)
    elif t == "int":
        ok = (isinstance(value, int) and not isinstance(value, bool)
              and (sdef.min is None or value >= sdef.min) and (sdef.max is None or value <= sdef.max))
    elif t == "choice":
        ok = isinstance(value, str) and value in {c for c, _ in sdef.choices}
    elif t == "list":
        if not isinstance(value, list):
            return sdef.default, False
        clean = coerce(sdef, [x for x in value if isinstance(x, str)])
        return clean, clean == value
    else:
        ok = isinstance(value, str)
    return (value, True) if ok else (sdef.default, False)


def _load(sdef: SettingDef, row: SettingValue) -> Any:
    try:
        return sanitize(sdef, json.loads(row.value))[0]
    except ValueError:  # битый JSON
        return sdef.default


def get_setting(db: Session, key: str, user_id: int | None = None) -> Any:
    sdef = REGISTRY[key]
    if sdef.scope == "user" and user_id is not None:
        row = _row(db, key, user_id)
        if row:
            return _load(sdef, row)
    row = _row(db, key, None)
    return _load(sdef, row) if row else sdef.default


def stale_rows(db: Session) -> list[tuple[SettingValue, str]]:
    """Записи настроек, не подходящие к нынешнему реестру: (запись, причина). Значения не возвращаются наружу."""
    found = []
    for row in db.scalars(select(SettingValue).order_by(SettingValue.key, SettingValue.id)).all():
        sdef = REGISTRY.get(row.key)
        if sdef is None:
            found.append((row, "нет в реестре"))
        elif row.user_id is not None and sdef.scope != "user":
            found.append((row, "личное значение глобальной настройки"))
        else:
            try:
                ok = sanitize(sdef, json.loads(row.value))[1]
            except ValueError:
                ok = False
            if not ok:
                found.append((row, "значение устарело или не того типа"))
    return found


def prune_stale(db: Session) -> int:
    """Удаляет неизвестные записи и личные значения глобальных; устаревшие — приводит (список) или сбрасывает к default."""
    rows = stale_rows(db)
    for row, reason in rows:
        sdef = REGISTRY.get(row.key)
        if reason == "значение устарело или не того типа" and sdef.type == "list":
            try:
                clean, _ = sanitize(sdef, json.loads(row.value))
                row.value = json.dumps(clean)
                continue
            except ValueError:
                pass
        db.delete(row)
    db.commit()
    return len(rows)


def set_setting(db: Session, key: str, value: Any, user_id: int | None = None) -> None:
    sdef = REGISTRY[key]
    uid = user_id if sdef.scope == "user" else None
    row = _row(db, key, uid)
    if row is None:
        row = SettingValue(key=key, user_id=uid)
        db.add(row)
    row.value = json.dumps(value)
    db.commit()


def section_values(db: Session, section: str, user_id: int | None) -> list[tuple[SettingDef, Any]]:
    return [(s, get_setting(db, s.key, user_id)) for s in SETTINGS if s.section == section]
