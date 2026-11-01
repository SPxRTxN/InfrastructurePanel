"""Журнал безопасности: события (выход, пароли, пользователи, агенты, удаления) и уборка старых записей.

Попытки входа пишет `routers/auth.py` в `login_attempts` — там же блокировка перебора. Пароли, токены и
ключи в журнал не попадают никогда: только факт события, логин, IP и короткая деталь.
"""
from datetime import timedelta

from fastapi import Request
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import LoginAttempt, Notification, ReminderLog, SecurityEvent, UserSession
from app.services.settings_registry import get_setting
from app.utils import utcnow

KINDS = {
    "logout": "Выход",
    "password_change": "Смена пароля",
    "setup": "Первая настройка администратора",
    "user_create": "Пользователь создан",
    "user_update": "Пользователь изменён",
    "user_delete": "Пользователь удалён",
    "session_revoke": "Сессия завершена",
    "agent_auth": "Подключение агента",
    "node_token": "Выдан токен узла",
    "delete": "Удаление",
    "archive_download": "Скачан архив / история",
    "alert_config": "Настройка оповещений",
    "vpn_config": "Настройка VPN",
    "device_action": "Действие над устройством",
    "config_export": "Экспорт конфигурации",
    "config_import": "Импорт конфигурации",
    "backup": "Бэкап не удался",
    "backup_verify": "Проверка восстановления не прошла",
    "backup_config": "Настройка бэкапов",
    "panel_update": "Обновление панели",
    "agent_config": "Настройки агента из панели",
    "agent_uninstall": "Удаление агента с узла",
    "console_open": "SSH-консоль открыта",
    "console_close": "SSH-консоль закрыта",
    "recording_view": "Открыта запись консоли",
    "recording_download": "Скачана запись консоли",
    "recording_delete": "Удалена запись консоли",
    "ssh_access": "SSH-доступ: входы и ключи хостов",
    "file_upload": "Файл передан на сервер",
    "node_server": "Создана карточка сервера для узла",
}


def log_event(db: Session, kind: str, ok: bool = True, request: Request | None = None, username: str = "",
              detail: str = "", ip: str = "", commit: bool = True) -> None:
    if request is not None:
        ip = ip or (request.client.host if request.client else "")
        ua = request.headers.get("user-agent", "")[:255]
    else:
        ua = ""
    db.add(SecurityEvent(kind=kind[:32], ok=ok, username=username[:64], ip=ip[:64], user_agent=ua,
                         detail=detail[:500]))
    if commit:
        db.commit()


def cleanup(db: Session) -> None:
    """Сроки хранения (аудит A-07): попытки входа и события — `security.log_days`; прочитанные уведомления и
    отметки отправленных напоминаний — 30 дней (напоминание старше 10 минут всё равно не шлётся); просроченные
    сессии — сразу."""
    now = utcnow()
    border = now - timedelta(days=get_setting(db, "security.log_days"))
    month = now - timedelta(days=30)
    db.execute(delete(LoginAttempt).where(LoginAttempt.created_at < border))
    db.execute(delete(SecurityEvent).where(SecurityEvent.ts < border))
    db.execute(delete(Notification).where(Notification.is_read.is_(True), Notification.created_at < month))
    db.execute(delete(ReminderLog).where(ReminderLog.sent_at < month))
    db.execute(delete(UserSession).where(UserSession.expires_at < now))
    db.commit()
