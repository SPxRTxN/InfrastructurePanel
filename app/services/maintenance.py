"""Режим обслуживания и обновление панели через хост-службу `homelab-updater`.

Панель в контейнере Docker не управляет. Она кладёт запрос в `<каталог БД>/update/request.json` и включает
режим обслуживания; хост-служба (tools/update-from-panel.sh → tools/deploy.sh) выполняет обновление и пишет
ход в `log.txt`, а итог — в `status.json`. После старта (и раз в 15 с фоном) панель читает итог и выключает режим.

Файлы обмена: request.json (пишет панель), status.json и log.txt (пишет служба; `requested` — панель),
history.jsonl (длительности прошлых обновлений для ETA).
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import config
from app.db import SessionLocal, sqlite_path
from app.models import Maintenance, Notification, User, UserSession
from app.services import audit
from app.services.security import hash_token
from app.utils import utcnow

log = logging.getLogger("uvicorn.error")

# Тег образа: X.Y.Z (с необязательным v) или latest. Тот же регэксп проверяет и хост-скрипт.
VERSION_RE = re.compile(r"^(v?\d+\.\d+\.\d+|latest)$")
DEFAULT_ETA = 180  # секунд, если истории обновлений ещё нет
REQUESTED_TIMEOUT = timedelta(minutes=10)  # служба не взяла запрос
RUNNING_TIMEOUT = timedelta(minutes=45)  # служба взяла, но итога нет
NO_UPDATER = "служба обновления не запущена (см. инструкцию: Настройки → Обновление)"
# Пути, которые работают всегда: проверки, статика, вход, агенты (их данные не должны теряться)
OPEN_PREFIXES = ("/healthz", "/static", "/login", "/logout", "/setup", "/agent/ws")
LOG_TAIL = 30_000  # байт лога, отдаваемых странице
STEPS = {"start": "запуск", "backup": "бэкап БД", "pull": "загрузка образа", "up": "запуск контейнера",
         "healthy": "ожидание готовности", "rollback": "откат", "finish": "завершено", "request": "запрос",
         "requested": "ожидание службы", "deploy": "выкладка"}


# ---------- файлы обмена ----------

def update_dir() -> Path | None:
    """Каталог обмена рядом с БД (в Docker — /data/update); None, если БД не SQLite-файл."""
    db = sqlite_path(config.database_url)
    return db.parent / "update" if db else None


def _write_json(path: Path, data: dict) -> None:
    """Атомарно: служба не должна увидеть половину файла."""
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.chmod(0o644)
    tmp.replace(path)


def updater_info() -> dict | None:
    """Отметка, которую оставляет tools/install-updater.sh: служба обновления на хосте установлена.
    Панель в контейнере не видит systemd хоста — это единственный способ показать состояние на странице."""
    d = update_dir()
    try:
        data = json.loads((d / "updater.json").read_text())
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(data, dict) or not data.get("installed_at"):
        return None
    return {"installed_at": str(data["installed_at"])[:40]}


def read_status() -> dict | None:
    d = update_dir()
    try:
        data = json.loads((d / "status.json").read_text())
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(data, dict) or data.get("state") not in ("requested", "running", "done", "failed"):
        return None
    return {k: str(data.get(k, ""))[:300] for k in ("state", "step", "version", "started", "ts", "error")}


def read_log() -> str:
    d = update_dir()
    try:
        with open(d / "log.txt", "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def eta_seconds() -> int:
    """Среднее по последним удачным обновлениям (history.jsonl), иначе 3 минуты; не меньше минуты."""
    d = update_dir()
    secs = []
    try:
        for line in (d / "history.jsonl").read_text().splitlines()[-10:]:
            row = json.loads(line)
            if row.get("state") == "done" and isinstance(row.get("seconds"), int):
                secs.append(row["seconds"])
    except (OSError, ValueError, AttributeError):
        pass
    return max(60, round(sum(secs) / len(secs))) if secs else DEFAULT_ETA


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


# ---------- состояние в БД ----------

def get(db: Session) -> Maintenance | None:
    return db.get(Maintenance, 1)


def is_on(db: Session) -> bool:
    row = get(db)
    return bool(row and row.enabled)


def local_hhmm(moment: datetime | None) -> str:
    """UTC из БД → время сервера для «примерно до HH:MM»."""
    if not moment:
        return ""
    return moment.replace(tzinfo=timezone.utc).astimezone().strftime("%H:%M")


def start_update(db: Session, username: str, version: str) -> None:
    """Пишет запрос службе и включает режим обслуживания. ValueError — текст для администратора."""
    version = version or ""
    if not VERSION_RE.fullmatch(version):
        raise ValueError("Версия: X.Y.Z (например, 1.4.0 или v1.4.0) или latest")
    if is_on(db):
        raise ValueError("Обновление уже идёт")
    d = update_dir()
    if d is None:
        raise ValueError("Обновление работает только с БД SQLite в каталоге данных")
    now = utcnow()
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        # Прошлые итоги убираем: иначе новая панель увидит старый done/failed. Каталог принадлежит панели,
        # поэтому удалить файлы, созданные службой (root), можно.
        for name in ("status.json", "log.txt", "request.json"):
            (d / name).unlink(missing_ok=True)
        _write_json(d / "status.json", {"state": "requested", "step": "requested", "version": version,
                                       "started": "", "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "error": ""})
        # Служба берёт из файла только version; остальное — для людей
        _write_json(d / "request.json", {"version": version, "requested_by": username,
                                         "requested_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    except OSError as e:
        raise ValueError(f"Не удалось записать запрос в {d}: {e.strerror or e}") from e
    row = get(db) or Maintenance(id=1)
    row.enabled, row.since, row.by = True, now, username[:64]
    row.eta = now + timedelta(seconds=eta_seconds())
    row.target_version = version
    row.message = f"Технические работы: обновление панели до {version}"
    db.add(row)
    audit.log_event(db, "panel_update", username=username, detail=f"запрос обновления до {version}")


def cancel(db: Session, username: str) -> bool:
    """Отмена, пока служба не взяла запрос (request.json на месте и статус requested)."""
    row = get(db)
    d = update_dir()
    st = read_status()
    if not (row and row.enabled and d and st and st["state"] == "requested" and (d / "request.json").exists()):
        return False
    (d / "request.json").unlink(missing_ok=True)
    (d / "status.json").unlink(missing_ok=True)
    row.enabled = False
    audit.log_event(db, "panel_update", username=username, detail="обновление отменено", commit=False)
    db.commit()
    return True


def _finish(db: Session, row: Maintenance, ok: bool, detail: str) -> None:
    row.enabled = False
    title = "Панель обновлена" if ok else "Обновление панели не удалось"
    audit.log_event(db, "panel_update", ok=ok, username=row.by, detail=detail, commit=False)
    for u in db.scalars(select(User).where(User.is_active.is_(True))):
        if u.can("settings.manage"):
            db.add(Notification(user_id=u.id, title=title, body=detail[:1000], link="/settings/update"))
    db.commit()


def check(db: Session) -> None:
    """Читает итог от службы и выключает режим. Вызывается при старте и фоновой задачей."""
    row = get(db)
    if not (row and row.enabled):
        return
    st = read_status()
    now = utcnow()
    ver = row.target_version
    if st and st["state"] == "done":
        _finish(db, row, True, f"обновление до {ver} выполнено")
    elif st and st["state"] == "failed":
        step = STEPS.get(st["step"], st["step"])
        _finish(db, row, False, f"обновление до {ver} не удалось на шаге «{step}»: {st['error'] or 'см. журнал'}")
    elif st and st["state"] == "running":
        started = _parse_ts(st["started"]) or row.since
        if now - started > RUNNING_TIMEOUT:
            _finish(db, row, False, f"обновление до {ver}: нет итога от службы более "
                                    f"{int(RUNNING_TIMEOUT.total_seconds() // 60)} минут, режим выключен")
    else:
        # requested или файла нет совсем (например, БД восстановлена из копии): ждём службу ограниченное время
        since = (_parse_ts(st["ts"]) if st else None) or row.since
        if now - since > REQUESTED_TIMEOUT:
            d = update_dir()
            if d:
                (d / "request.json").unlink(missing_ok=True)  # запоздавшая служба не должна запуститься сама
                try:
                    _write_json(d / "status.json", {"state": "failed", "step": "requested", "version": ver,
                                                   "started": "", "error": NO_UPDATER,
                                                   "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
                except OSError:
                    pass
            _finish(db, row, False, f"обновление до {ver} не началось: {NO_UPDATER}")


def check_safely() -> None:
    """Для lifespan: ошибка здесь не должна мешать запуску панели."""
    try:
        with SessionLocal() as db:
            check(db)
    except Exception:
        log.exception("Проверка итога обновления")


# ---------- middleware ----------

def _can_manage(request: Request, db: Session) -> bool:
    """Вошёл ли с settings.manage. Своя проверка, а не web.load_session: кэш в request.state пережил бы
    закрытие этой сессии БД и сломал бы обработчик."""
    token = request.cookies.get("hl_session")
    if not token:
        return False
    s = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    return bool(s and s.expires_at >= utcnow() and s.user.is_active and s.user.can("settings.manage"))


def _blocked(request: Request) -> Maintenance | None:
    """Строка режима, если запрос надо остановить; None — пропускаем."""
    path = request.url.path
    if path.startswith(OPEN_PREFIXES):
        return None
    try:
        with SessionLocal() as db:
            row = get(db)
            if not (row and row.enabled) or _can_manage(request, db):
                return None
            db.expunge(row)
            return row
    except Exception:  # до миграции таблицы нет — режима нет
        return None


async def middleware(request: Request, call_next):
    row = _blocked(request)
    if row is None:
        return await call_next(request)
    left = int((row.eta - utcnow()).total_seconds()) if row.eta else 0
    headers = {"Retry-After": str(max(30, left))}
    until = local_hhmm(row.eta)
    accept = request.headers.get("accept", "")
    if request.url.path.startswith("/api") or "application/json" in accept or request.headers.get("hx-request"):
        return JSONResponse({"detail": "Технические работы" + (f", примерно до {until}" if until else ""),
                             "maintenance": True, "until": until}, 503, headers=headers)
    from app.web import render  # позже, чтобы не тянуть шаблоны при импорте сервиса

    resp = render(request, "maintenance.html", {"title": "Технические работы", "until": until,
                                                "message": row.message}, None, None, status_code=503)
    resp.headers.update(headers)
    return resp
