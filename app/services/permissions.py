"""Справочник прав, группы и администратор по умолчанию."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import config
from app.models import Group, GroupPermission, User
from app.services.security import hash_password

SECTIONS = {
    "servers": "Серверы",
    "jira": "Jira",
    "confluence": "Confluence",
    "calendar": "Календарь",
    "topology": "Топология",
}

PERMISSIONS: dict[str, str] = {}
for _key, _label in SECTIONS.items():
    PERMISSIONS[f"{_key}.view"] = f"{_label}: просмотр"
    PERMISSIONS[f"{_key}.edit"] = f"{_label}: изменение"
# Интерактивный shell на сервере — сильнее любого .edit, поэтому отдельное право и только у администраторов
PERMISSIONS["servers.console"] = "Серверы: SSH-консоль"
PERMISSIONS["settings.manage"] = "Глобальные настройки"
PERMISSIONS["users.manage"] = "Пользователи и группы"

VIEW_PERMS = [f"{k}.view" for k in SECTIONS]
EDIT_PERMS = [f"{k}.edit" for k in SECTIONS]

DEFAULT_GROUPS = {
    "Администраторы": ("Полный доступ", list(PERMISSIONS)),
    "Пользователи": ("Просмотр и изменение, без настроек", VIEW_PERMS + EDIT_PERMS),
    "Наблюдатели": ("Только просмотр", VIEW_PERMS),
}
ADMIN_GROUP = "Администраторы"


def ensure_default_groups(db: Session) -> None:
    """Создаёт встроенные группы, если их нет. Права существующих групп не трогает."""
    for name, (descr, perms) in DEFAULT_GROUPS.items():
        if db.scalar(select(Group).where(Group.name == name)):
            continue
        group = Group(name=name, description=descr, builtin=True)
        group.permissions = [GroupPermission(perm=p) for p in perms]
        db.add(group)
    db.commit()


DEFAULT_ADMIN = "admin"


def ensure_default_admin(db: Session) -> bool:
    """На пустой БД (первое развёртывание) создаёт admin/admin (или admin/FIRST_ADMIN_PASSWORD)
    с обязательной сменой логина и пароля.

    Если в БД есть хоть один пользователь — ничего не делает: перезапуск и обновление
    контейнера не пересоздают и не сбрасывают учётные записи.
    """
    if db.scalar(select(func.count()).select_from(User)):
        return False
    admin_group = db.scalar(select(Group).where(Group.name == ADMIN_GROUP))
    # На сервере, открытом в интернет, admin/admin угадают боты раньше владельца — пароль можно задать в .env
    password = config.first_admin_password or DEFAULT_ADMIN
    db.add(User(username=DEFAULT_ADMIN, password_hash=hash_password(password),
                must_change_credentials=True, groups=[admin_group]))
    db.commit()
    return True
