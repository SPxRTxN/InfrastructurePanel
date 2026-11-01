from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import config
from app.db import get_db
from app.models import LoginAttempt, User, UserSession
from app.services import audit
from app.services.security import (MIN_PASSWORD_LEN, hash_password, hash_token, needs_rehash, new_token,
                                   verify_password)
from app.utils import utcnow
from app.web import LOGIN_CSRF_COOKIE, SESSION_COOKIE, SETUP_PATH, current_user, load_session, render

router = APIRouter()

MAX_FAILS = 5        # на пару логин + IP
IP_MAX_FAILS = 20    # на IP по всем логинам
LOCK_WINDOW = timedelta(minutes=15)


def safe_next(url: str) -> str:
    # Только локальные пути: защита от open redirect через ?next=//evil.com
    return url if url.startswith("/") and not url.startswith("//") and "\\" not in url else "/"


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _login_page(request: Request, next_url: str, error: str = "", status_code: int = 200):
    token = new_token()
    resp = render(request, "login.html", {"csrf_token": token, "next": next_url, "error": error},
                  status_code=status_code)
    resp.set_cookie(LOGIN_CSRF_COOKIE, token, httponly=True, samesite="lax",
                    secure=config.cookie_secure, max_age=3600)
    return resp


@router.get("/login")
def login_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
    if load_session(request, db):
        return RedirectResponse("/", 303)
    return _login_page(request, safe_next(next))


@router.post("/login")
def login(
    request: Request,
    username: Annotated[str, Form(max_length=64)],
    password: Annotated[str, Form(max_length=1024)],
    next: Annotated[str, Form()] = "/",
    db: Session = Depends(get_db),
):
    username = username.strip()
    ip = client_ip(request)
    ua = request.headers.get("user-agent", "")[:255]
    next_url = safe_next(next)
    user = db.scalar(select(User).where(User.username == username))
    # Несуществующий логин не сохраняем: туда часто по ошибке набирают пароль (аудит S-06).
    # Все такие попытки с одного IP считаются вместе — блокировка перебора продолжает работать.
    logged_name = user.username if user else ""

    fails = db.scalar(
        select(func.count()).select_from(LoginAttempt).where(
            LoginAttempt.username == logged_name,
            LoginAttempt.ip == ip,
            LoginAttempt.success.is_(False),
            LoginAttempt.created_at >= utcnow() - LOCK_WINDOW,
        )
    )
    # Перебор одного пароля по многим логинам: общий лимит на IP (аудит A-02)
    ip_fails = db.scalar(
        select(func.count()).select_from(LoginAttempt).where(
            LoginAttempt.ip == ip, LoginAttempt.success.is_(False), LoginAttempt.created_at >= utcnow() - LOCK_WINDOW,
        )
    )
    if fails >= MAX_FAILS or ip_fails >= IP_MAX_FAILS:
        reason = "locked" if fails >= MAX_FAILS else "ip_locked"
        db.add(LoginAttempt(username=logged_name, ip=ip, user_agent=ua, success=False, reason=reason))
        db.commit()
        return _login_page(request, next_url, "Слишком много попыток. Повторите через 15 минут.", 429)

    ok = verify_password(user.password_hash if user else None, password) and user.is_active
    reason = "ok" if ok else ("inactive" if user and not user.is_active else "bad_credentials")
    db.add(LoginAttempt(username=logged_name, ip=ip, user_agent=ua, success=ok, reason=reason))
    if not ok:
        db.commit()
        return _login_page(request, next_url, "Неверный логин или пароль", 401)

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)  # плавный переход на перец/новые параметры argon2
    db.execute(delete(UserSession).where(UserSession.expires_at < utcnow()))
    # Прежняя сессия этого браузера (например, другого пользователя) больше не нужна (аудит A-10)
    old = request.cookies.get(SESSION_COOKIE)
    if old:
        db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(old)))
    token = new_session(db, user, ip, ua)
    db.commit()
    resp = RedirectResponse(next_url, 303)
    set_session_cookie(resp, token)
    resp.delete_cookie(LOGIN_CSRF_COOKIE)
    return resp


def new_session(db: Session, user: User, ip: str, ua: str) -> str:
    """Новая сессия: в БД — хеш, токен — в cookie. Коммит — у вызывающего."""
    token = new_token()
    db.add(UserSession(
        token_hash=hash_token(token), csrf_token=new_token(), user_id=user.id, ip=ip, user_agent=ua,
        expires_at=utcnow() + timedelta(hours=config.session_hours),
    ))
    return token


def set_session_cookie(resp, token: str) -> None:
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=config.cookie_secure,
                    max_age=config.session_hours * 3600)


def _setup_page(request: Request, db: Session, user: User, error: str = "", username: str = "",
                status_code: int = 200):
    ctx = {"csrf_token": load_session(request, db).csrf_token, "error": error, "username": username,
           "min_len": MIN_PASSWORD_LEN}
    return render(request, "setup.html", ctx, status_code=status_code)


@router.get(SETUP_PATH)
def setup_form(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not user.must_change_credentials:
        return RedirectResponse("/", 303)
    return _setup_page(request, db, user)


@router.post(SETUP_PATH)
def setup(
    request: Request,
    username: Annotated[str, Form(max_length=64)],
    password: Annotated[str, Form(max_length=1024)],
    password2: Annotated[str, Form(max_length=1024)],
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Первый вход администратора по умолчанию: обязательная смена и логина, и пароля."""
    if not user.must_change_credentials:
        return RedirectResponse("/", 303)
    username = username.strip()
    error = ""
    if not username or username.casefold() == user.username.casefold():
        error = "Укажите новый логин — не такой, как сейчас"
    elif db.scalar(select(User).where(User.username == username, User.id != user.id)):
        error = "Такой логин уже занят"
    elif len(password) < MIN_PASSWORD_LEN:
        error = f"Пароль должен быть не короче {MIN_PASSWORD_LEN} символов"
    elif password != password2:
        error = "Пароли не совпадают"
    elif verify_password(user.password_hash, password):
        error = "Новый пароль должен отличаться от текущего"
    if error:
        return _setup_page(request, db, user, error, username, 400)

    user.username = username
    user.password_hash = hash_password(password)
    user.must_change_credentials = False
    # Если кто-то ещё успел войти под admin/admin — его сессии больше не действуют
    current = load_session(request, db)
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user.id, UserSession.id != current.id)):
        db.delete(s)
    audit.log_event(db, "setup", request=request, username=username, detail="логин и пароль администратора заданы",
                    commit=False)
    db.commit()
    return RedirectResponse("/", 303)


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    row = load_session(request, db)
    if row:
        audit.log_event(db, "logout", request=request, username=row.user.username, commit=False)
        db.delete(row)
        db.commit()
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
