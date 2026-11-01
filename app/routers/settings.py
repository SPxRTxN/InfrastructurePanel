import csv
import io
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Group, GroupPermission, LoginAttempt, SecurityEvent, User, UserSession
from app.routers.auth import LOCK_WINDOW, MAX_FAILS, client_ip, new_session, set_session_cookie
from app.services import audit
from app.services.permissions import PERMISSIONS
from app.services.security import MIN_PASSWORD_LEN, hash_password, verify_password
from app.services.settings_registry import REGISTRY, SECTIONS, coerce, get_setting, section_values, set_setting
from app.utils import utcnow
from app.web import current_user, load_session, render, require

router = APIRouter(prefix="/settings")

# (slug, название, право для показа; None — любому вошедшему)
NAV = [
    ("profile", "Профиль", None),
    ("users", "Пользователи", "users.manage"),
    ("groups", "Группы и права", "users.manage"),
    ("servers", "Серверы и сервисы", "servers.view"),
    ("appearance", "Внешний вид", None),
    ("home", "Главная", None),
    ("calendar", "Календарь", None),
    ("modules", "Разделы", "settings.manage"),
    ("security", "Безопасность", "users.manage"),
    ("backups", "Бэкапы", "settings.manage"),
    ("transfer", "Перенос", "settings.manage"),
    ("update", "Обновление", "settings.manage"),
]

# Хуки для разделов, которым нужен доп. контекст (например, список серверов). Заполняются модулями.
SECTION_EXTRA: dict[str, callable] = {}


def _page(request, db, user, tpl, section, status_code=200, **ctx):
    nav = [n for n in NAV if n[2] is None or user.can(n[2])]
    return render(request, tpl, {"nav": nav, "section": section, "title": "Настройки", **ctx}, user, db,
                  status_code=status_code)


@router.get("")
def settings_root(user: User = Depends(current_user)):
    return RedirectResponse("/settings/profile", 303)


@router.post("/sidebar")
def toggle_sidebar(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Сохраняет состояние сайдбара; вызывается из JS при нажатии кнопки свёртывания."""
    set_setting(db, "appearance.sidebar_collapsed",
                not get_setting(db, "appearance.sidebar_collapsed", user.id), user.id)
    return Response(status_code=204)


# ---------- Профиль ----------

@router.get("/profile")
def profile(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _page(request, db, user, "settings/profile.html", "profile")


@router.post("/profile")
def profile_save(request: Request, display_name: Annotated[str, Form(max_length=128)] = "",
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.display_name = display_name.strip()
    db.commit()
    return RedirectResponse("/settings/profile?saved=1", 303)


@router.post("/profile/password")
def change_password(
    request: Request,
    old_password: Annotated[str, Form(max_length=1024)],
    new_password: Annotated[str, Form(max_length=1024)],
    new_password2: Annotated[str, Form(max_length=1024)],
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    error = ""
    if not verify_password(user.password_hash, old_password):
        error = "Текущий пароль неверен"
    elif len(new_password) < MIN_PASSWORD_LEN:
        error = f"Новый пароль короче {MIN_PASSWORD_LEN} символов"
    elif new_password != new_password2:
        error = "Пароли не совпадают"
    if error:
        audit.log_event(db, "password_change", ok=False, request=request, username=user.username, detail=error)
        return _page(request, db, user, "settings/profile.html", "profile", error=error, status_code=400)
    user.password_hash = hash_password(new_password)
    audit.log_event(db, "password_change", request=request, username=user.username,
                    detail="остальные сессии завершены", commit=False)
    # Все сессии пользователя завершаем, включая текущую, и выдаём новый токен: смена пароля выкидывает
    # чужие входы, а украденная до смены cookie текущего браузера тоже перестаёт работать (аудит A-10)
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
        db.delete(s)
    token = new_session(db, user, client_ip(request), request.headers.get("user-agent", "")[:255])
    db.commit()
    resp = RedirectResponse("/settings/profile?pwd=1", 303)
    set_session_cookie(resp, token)
    return resp


# ---------- Пользователи ----------

def _ensure_admin_left(db: Session) -> bool:
    """Должен остаться хотя бы один активный пользователь с users.manage."""
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    return any(u.can("users.manage") for u in users)


def _users_page(request, db, user, error="", status_code=200):
    users = db.scalars(select(User).order_by(User.username)).all()
    groups = db.scalars(select(Group).order_by(Group.name)).all()
    log = db.scalars(select(LoginAttempt).order_by(LoginAttempt.id.desc()).limit(50)).all()
    return _page(request, db, user, "settings/users.html", "users", users=users, groups=groups, log=log,
                 error=error, status_code=status_code)


@router.get("/users")
def users_list(request: Request, user: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    return _users_page(request, db, user)


@router.post("/users")
async def user_create(request: Request, user: User = Depends(require("users.manage")),
                      db: Session = Depends(get_db)):
    form = await request.form()
    username = str(form.get("username", "")).strip()[:64]
    password = str(form.get("password", ""))
    if not username or len(password) < MIN_PASSWORD_LEN:
        return _users_page(request, db, user, f"Нужен логин и пароль от {MIN_PASSWORD_LEN} символов", 400)
    if db.scalar(select(User).where(User.username == username)):
        return _users_page(request, db, user, "Такой логин уже есть", 400)
    group_ids = [int(g) for g in form.getlist("groups") if str(g).isdigit()]
    new = User(username=username, display_name=str(form.get("display_name", "")).strip()[:128],
               password_hash=hash_password(password))
    new.groups = list(db.scalars(select(Group).where(Group.id.in_(group_ids))))
    db.add(new)
    audit.log_event(db, "user_create", request=request, username=user.username,
                    detail=f"«{username}», группы: {', '.join(g.name for g in new.groups) or 'нет'}", commit=False)
    db.commit()
    return RedirectResponse("/settings/users", 303)


@router.post("/users/{user_id}")
async def user_update(user_id: int, request: Request, user: User = Depends(require("users.manage")),
                      db: Session = Depends(get_db)):
    target = db.get(User, user_id) or _not_found()
    form = await request.form()
    target.display_name = str(form.get("display_name", target.display_name)).strip()[:128]
    target.is_active = form.get("is_active") == "on"
    group_ids = [int(g) for g in form.getlist("groups") if str(g).isdigit()]
    target.groups = list(db.scalars(select(Group).where(Group.id.in_(group_ids))))
    password = str(form.get("password", ""))
    if password:
        if len(password) < MIN_PASSWORD_LEN:
            db.rollback()
            return _users_page(request, db, user, f"Пароль короче {MIN_PASSWORD_LEN} символов", 400)
        target.password_hash = hash_password(password)
    db.flush()
    if not _ensure_admin_left(db):
        db.rollback()
        return _users_page(request, db, user, "Нельзя убрать последнего администратора", 400)
    if not target.is_active or password:
        for s in db.scalars(select(UserSession).where(UserSession.user_id == target.id)):
            if s.id != load_session(request, db).id:
                db.delete(s)
    changes = [f"группы: {', '.join(g.name for g in target.groups) or 'нет'}",
               "активен" if target.is_active else "отключён"] + (["пароль сменён"] if password else [])
    audit.log_event(db, "user_update", request=request, username=user.username,
                    detail=f"«{target.username}»: " + ", ".join(changes), commit=False)
    db.commit()
    return RedirectResponse("/settings/users", 303)


@router.post("/users/{user_id}/delete")
def user_delete(user_id: int, request: Request, user: User = Depends(require("users.manage")),
                db: Session = Depends(get_db)):
    """Удаление пользователя. Группы, сессии, личные настройки и уведомления удаляются каскадом в БД;
    в задачах, статьях и событиях исполнитель/автор становится пустым; журнал входов остаётся как история."""
    target = db.get(User, user_id) or _not_found()
    if target.id == user.id:
        return _users_page(request, db, user, "Нельзя удалить самого себя", 400)
    db.delete(target)
    db.flush()
    if not _ensure_admin_left(db):
        db.rollback()
        return _users_page(request, db, user, "Нельзя удалить последнего администратора", 400)
    audit.log_event(db, "user_delete", request=request, username=user.username, detail=f"«{target.username}»",
                    commit=False)
    db.commit()
    return RedirectResponse("/settings/users", 303)


def _not_found():
    raise HTTPException(404, "Не найдено")


# ---------- Группы ----------

def _groups_page(request, db, user, error="", status_code=200):
    groups = db.scalars(select(Group).order_by(Group.name)).all()
    return _page(request, db, user, "settings/groups.html", "groups", groups=groups, permissions=PERMISSIONS,
                 error=error, status_code=status_code)


@router.get("/groups")
def groups_list(request: Request, user: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    return _groups_page(request, db, user)


@router.post("/groups")
def group_create(request: Request, name: Annotated[str, Form(max_length=64)],
                 description: Annotated[str, Form(max_length=255)] = "",
                 user: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    name = name.strip()
    if not name or db.scalar(select(Group).where(Group.name == name)):
        return _groups_page(request, db, user, "Пустое или занятое имя группы", 400)
    db.add(Group(name=name, description=description.strip()))
    db.commit()
    return RedirectResponse("/settings/groups", 303)


@router.post("/groups/{group_id}")
async def group_update(group_id: int, request: Request, user: User = Depends(require("users.manage")),
                       db: Session = Depends(get_db)):
    group = db.get(Group, group_id) or _not_found()
    form = await request.form()
    wanted = {p for p in form.getlist("perms") if p in PERMISSIONS}
    # Сохраняем существующие строки: замена целиком дала бы INSERT раньше DELETE и конфликт уникальности
    kept = [gp for gp in group.permissions if gp.perm in wanted]
    have = {gp.perm for gp in kept}
    group.permissions = kept + [GroupPermission(perm=p) for p in sorted(wanted - have)]
    db.flush()
    if not _ensure_admin_left(db):
        db.rollback()
        return _groups_page(request, db, user, "Нельзя убрать право управления у последнего администратора", 400)
    audit.log_event(db, "user_update", request=request, username=user.username,
                    detail=f"права группы «{group.name}»: {', '.join(sorted(wanted)) or 'нет'}", commit=False)
    db.commit()
    return RedirectResponse("/settings/groups", 303)


@router.post("/groups/{group_id}/delete")
def group_delete(group_id: int, request: Request, user: User = Depends(require("users.manage")),
                 db: Session = Depends(get_db)):
    group = db.get(Group, group_id) or _not_found()
    if group.builtin:
        return _groups_page(request, db, user, "Встроенную группу удалить нельзя", 400)
    db.delete(group)
    db.commit()
    return RedirectResponse("/settings/groups", 303)


# ---------- Безопасность: журнал входов, события, сессии ----------

def _security_context(db: Session, request: Request, fails_only: bool, q: str) -> dict:
    now = utcnow()
    day = now - timedelta(hours=24)
    attempts = select(LoginAttempt).order_by(LoginAttempt.id.desc()).limit(200)
    if fails_only:
        attempts = attempts.where(LoginAttempt.success.is_(False))
    if q:
        like = f"%{q}%"
        attempts = attempts.where((LoginAttempt.username.like(like)) | (LoginAttempt.ip.like(like)))
    fails_day = LoginAttempt.success.is_(False) & (LoginAttempt.created_at >= day)
    top_ips = db.execute(select(LoginAttempt.ip, func.count()).where(fails_day).group_by(LoginAttempt.ip)
                         .order_by(func.count().desc()).limit(5)).all()
    # Заблокированные сейчас — та же логика, что при входе: MAX_FAILS неудач за LOCK_WINDOW на логин+IP
    locked = db.execute(select(LoginAttempt.username, LoginAttempt.ip, func.count())
                        .where(LoginAttempt.success.is_(False), LoginAttempt.created_at >= now - LOCK_WINDOW)
                        .group_by(LoginAttempt.username, LoginAttempt.ip)
                        .having(func.count() >= MAX_FAILS)).all()
    sessions = db.scalars(select(UserSession).where(UserSession.expires_at > now)
                          .order_by(UserSession.created_at.desc())).all()
    current = load_session(request, db)
    return {
        "kpi": {
            "fails": db.scalar(select(func.count()).select_from(LoginAttempt).where(fails_day)),
            "fail_ips": db.scalar(select(func.count(func.distinct(LoginAttempt.ip))).where(fails_day)),
            "agent_fails": db.scalar(select(func.count()).select_from(SecurityEvent).where(
                SecurityEvent.kind == "agent_auth", SecurityEvent.ok.is_(False), SecurityEvent.ts >= day)),
            "sessions": len(sessions),
        },
        "top_ips": top_ips, "locked": locked, "attempts": db.scalars(attempts).all(),
        "events": db.scalars(select(SecurityEvent).order_by(SecurityEvent.id.desc()).limit(200)).all(),
        "sessions": sessions, "current_session_id": current.id if current else None,
        "kinds": audit.KINDS, "fails_only": fails_only, "q": q,
    }


@router.get("/security")
def security_view(request: Request, fails: int = 0, q: str = "", user: User = Depends(require("users.manage")),
                  db: Session = Depends(get_db)):
    q = q.strip()[:64]
    return _page(request, db, user, "settings/section.html", "security", section_title=SECTIONS["security"],
                 items=section_values(db, "security", user.id), **_security_context(db, request, bool(fails), q))


@router.get("/security/log.csv")
def security_csv(request: Request, user: User = Depends(require("users.manage")), db: Session = Depends(get_db)):
    """Журнал входов и событий за весь срок хранения — CSV (Excel откроет: UTF-8 с BOM, разделитель ;)."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["время (UTC)", "тип", "успех", "логин", "IP", "браузер/клиент", "подробности"])
    rows = [(a.created_at, "вход", a.success, a.username, a.ip, a.user_agent, a.reason)
            for a in db.scalars(select(LoginAttempt))]
    rows += [(e.ts, audit.KINDS.get(e.kind, e.kind), e.ok, e.username, e.ip, e.user_agent, e.detail)
             for e in db.scalars(select(SecurityEvent))]
    for ts, kind, ok, name, ip, ua, detail in sorted(rows, key=lambda r: r[0], reverse=True):
        # Ячейки, начинающиеся с = + - @, Excel считает формулой — экранируем (CSV-инъекция)
        cells = [ts.strftime("%Y-%m-%d %H:%M:%S"), kind, "да" if ok else "нет", name, ip, ua, detail]
        w.writerow(["'" + c if isinstance(c, str) and c[:1] in "=+-@" else c for c in cells])
    audit.log_event(db, "archive_download", request=request, username=user.username, detail="журнал безопасности CSV")
    return Response(("﻿" + out.getvalue()).encode(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="security-{utcnow():%Y%m%d-%H%M}.csv"',
                             "Cache-Control": "no-store"})


@router.post("/security/sessions/{session_id}/revoke")
def session_revoke(session_id: int, request: Request, user: User = Depends(require("users.manage")),
                   db: Session = Depends(get_db)):
    row = db.get(UserSession, session_id) or _not_found()
    current = load_session(request, db)
    if current and row.id == current.id:
        raise HTTPException(400, "Это ваша текущая сессия — для неё есть «Выход»")
    audit.log_event(db, "session_revoke", request=request, username=user.username,
                    detail=f"сессия «{row.user.username}» с {row.ip or '—'}", commit=False)
    db.delete(row)
    db.commit()
    return RedirectResponse("/settings/security#sessions", 303)


@router.post("/security/sessions/revoke-others")
def sessions_revoke_others(request: Request, user: User = Depends(require("users.manage")),
                           db: Session = Depends(get_db)):
    """Выкинуть всех, кроме себя, — например, если пароль мог утечь."""
    current = load_session(request, db)
    count = 0
    for row in db.scalars(select(UserSession).where(UserSession.id != current.id)):
        db.delete(row)
        count += 1
    audit.log_event(db, "session_revoke", request=request, username=user.username,
                    detail=f"завершены все сессии, кроме текущей: {count}", commit=False)
    db.commit()
    return RedirectResponse("/settings/security#sessions", 303)


# ---------- Разделы из реестра (внешний вид, главная, календарь, серверы) ----------

def _section_page(request, db, user, section, error="", status_code=200):
    if section not in SECTIONS:
        _not_found()
    if section == "servers" and not (user.can("servers.view") or user.can("settings.manage")):
        raise HTTPException(403, "Недостаточно прав")
    if section == "security" and not user.can("users.manage"):
        raise HTTPException(403, "Недостаточно прав")
    if section == "modules" and not user.can("settings.manage"):
        raise HTTPException(403, "Недостаточно прав")
    extra = SECTION_EXTRA[section](db, user) if section in SECTION_EXTRA else {}
    return _page(request, db, user, "settings/section.html", section, section_title=SECTIONS[section],
                 items=section_values(db, section, user.id), error=error, status_code=status_code, **extra)


@router.get("/{section}")
def section_view(section: str, request: Request, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    return _section_page(request, db, user, section)


@router.post("/{section}")
async def section_save(section: str, request: Request, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    if section not in SECTIONS:
        _not_found()
    form = await request.form()
    defs = [s for s in REGISTRY.values() if s.section == section]
    if any(s.scope == "global" for s in defs) and not user.can("settings.manage"):
        raise HTTPException(403, "Недостаточно прав")
    try:
        values = {}
        for s in defs:
            if s.type == "list":
                raw = form.getlist(s.key)
            elif s.type == "bool":
                raw = form.get(s.key, "")
            else:
                raw = form.get(s.key, s.default)
            values[s.key] = coerce(s, raw)
    except ValueError as e:
        return _section_page(request, db, user, section, str(e) or "Неверное значение", 400)
    for key, val in values.items():
        set_setting(db, key, val, user.id)
    return RedirectResponse(f"/settings/{section}?saved=1", 303)
