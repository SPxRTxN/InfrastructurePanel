"""Перенос конфигурации между панелями: экспорт выбранного в зашифрованный файл `.hlconf` и импорт с предпросмотром.

Пакет — JSON `{"format": "homelab-config", "version": 1, "exported_at", "meta", "sections": {...}}`, зашифрованный
паролем (age/scrypt через pyrage). Объекты связываются по именам, а не по id: id в другой панели другие.
Секреты (путь к SSH-ключу) внутри пакета лежат открытым текстом, но сам пакет зашифрован; при импорте они
перешифровываются ключом этой панели. Историю метрик, сессии, журналы, задачи, статьи и события не переносим.
"""
import hashlib
import hmac
import json
from datetime import datetime

from pyrage import DecryptError
from pyrage import passphrase as age
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import config
from app.models import (AlertRule, Device, Group, GroupPermission, Link, Node, Server, Service, SettingValue,
                        User)
from app.services import agents, alerts
from app.services.security import decrypt, encrypt, hash_password, hash_token, new_token
from app.services.settings_registry import REGISTRY
from app.utils import utcnow

FORMAT = "homelab-config"
VERSION = 1
FILE_EXT = ".hlconf"
MAX_FILE = 700 * 1024  # пакет проходит через скрытое поле формы подтверждения (лимит Starlette 1 МБ в base64)
MIN_PASSWORD = 8

SECTIONS = {
    "settings": "Глобальные настройки",
    "groups": "Группы и права",
    "users": "Пользователи",
    "servers": "Серверы и сервисы",
    "topology": "Карта сети",
    "nodes": "Узлы",
    "alerts": "Правила оповещений",
}
# Порядок применения: группы нужны пользователям, серверы — карте сети и узлам
ORDER = ["settings", "groups", "servers", "users", "topology", "nodes", "alerts"]
# Каналы оповещений (токен бота, chat id, webhook с секретом в адресе) и «тишина» не переносим никогда
EXCLUDED_SETTINGS = {"alerts.silence_until", "alerts.telegram_token", "alerts.telegram_chat", "alerts.webhook_url",
                     "alerts.webhook_kind"}
RULE_FIELDS = ("name", "kind", "threshold", "sensor", "op", "target", "for_minutes", "repeat_minutes", "severity",
               "enabled", "notify_bell", "notify_telegram", "notify_webhook")


class TransferError(Exception):
    """Понятная пользователю ошибка (неверный пароль, не тот файл, битые данные)."""


def _pepper_check() -> str:
    """Отпечаток перца: по нему при импорте видно, что хеши паролей из пакета здесь работать не будут."""
    if not config.password_pepper:
        return ""
    return hmac.new(config.password_pepper.encode(), b"homelab-transfer-check", hashlib.sha256).hexdigest()[:16]


# ---------- Сбор данных этой панели ----------

def _collect(db: Session, section: str, passwords: bool = True, same_tokens: bool = True):
    if section == "settings":
        rows = db.scalars(select(SettingValue).where(SettingValue.user_id.is_(None)).order_by(SettingValue.key)).all()
        return [{"key": r.key, "value": json.loads(r.value)} for r in rows
                if r.key in REGISTRY and r.key not in EXCLUDED_SETTINGS]
    if section == "groups":
        return [{"name": g.name, "description": g.description, "builtin": g.builtin,
                 "permissions": sorted(p.perm for p in g.permissions)}
                for g in db.scalars(select(Group).order_by(Group.name))]
    if section == "users":
        out = []
        for u in db.scalars(select(User).order_by(User.username)):
            item = {"username": u.username, "display_name": u.display_name, "is_active": u.is_active,
                    "groups": sorted(g.name for g in u.groups)}
            if passwords:
                item["password_hash"] = u.password_hash
            out.append(item)
        return out
    if section == "servers":
        return [{"name": s.name, "host": s.host, "ssh_port": s.ssh_port, "collector": s.collector,
                 "ssh_user": s.ssh_user, "enabled": s.enabled, "ssh_key": decrypt(s.ssh_key_enc),
                 "services": [{"name": v.name, "check_type": v.check_type, "target": v.target}
                              for v in sorted(s.services, key=lambda v: v.name)]}
                for s in db.scalars(select(Server).order_by(Server.name))]
    if section == "topology":
        devices = db.scalars(select(Device).order_by(Device.name, Device.id)).all()
        return {"devices": [{"name": d.name, "type": d.type, "ip": d.ip, "description": d.description,
                             "x": d.x, "y": d.y, "server": d.server.name if d.server else ""} for d in devices],
                "links": [{"a": k.a.name, "a_port": k.a_port, "b": k.b.name, "b_port": k.b_port, "label": k.label}
                          for k in db.scalars(select(Link).order_by(Link.id))]}
    if section == "nodes":
        out = []
        for n in db.scalars(select(Node).order_by(Node.name)):
            item = {"name": n.name, "server": n.server.name if n.server else ""}
            if same_tokens:
                item["token_hash"] = n.token_hash
            out.append(item)
        return out
    if section == "alerts":
        return [{f: getattr(r, f) for f in RULE_FIELDS} for r in db.scalars(select(AlertRule).order_by(AlertRule.id))]
    raise TransferError(f"Неизвестный раздел: {section}")


def build_package(db: Session, sections: set[str], passwords: bool = False, same_tokens: bool = False) -> dict:
    chosen = [s for s in ORDER if s in sections]
    if not chosen:
        raise TransferError("Отметьте хотя бы один раздел")
    return {
        "format": FORMAT, "version": VERSION, "exported_at": utcnow().isoformat(timespec="seconds"),
        "meta": {"passwords": passwords and "users" in chosen, "same_tokens": same_tokens and "nodes" in chosen,
                 "pepper": _pepper_check() if passwords and "users" in chosen else ""},
        "sections": {s: _collect(db, s, passwords, same_tokens) for s in chosen},
    }


# ---------- Шифрование ----------

def seal(package: dict, password: str) -> bytes:
    if len(password) < MIN_PASSWORD:
        raise TransferError(f"Пароль пакета — не короче {MIN_PASSWORD} символов")
    return age.encrypt(json.dumps(package, ensure_ascii=False).encode(), password)


def open_package(blob: bytes, password: str) -> dict:
    """Расшифровывает и проверяет схему. Любая ошибка — `TransferError` с понятным текстом."""
    if not blob:
        raise TransferError("Файл пустой")
    if len(blob) > MAX_FILE:
        raise TransferError(f"Файл больше {MAX_FILE // 1024} КБ — это не конфигурация панели")
    try:
        raw = age.decrypt(blob, password)
    except (DecryptError, ValueError, TypeError, OverflowError):
        # Неверный пароль и «не наш файл» неразличимы для человека — говорим про оба
        raise TransferError("Не удалось расшифровать: неверный пароль или это не файл .hlconf") from None
    try:
        package = json.loads(raw)
    except ValueError:
        raise TransferError("Внутри файла не JSON") from None
    return validate(package)


def validate(package) -> dict:
    if not isinstance(package, dict) or package.get("format") != FORMAT:
        raise TransferError("Это не пакет конфигурации Homelab")
    if package.get("version") != VERSION:
        raise TransferError(f"Версия пакета {package.get('version')} не поддерживается (нужна {VERSION})")
    sections = package.get("sections")
    if not isinstance(sections, dict) or not sections:
        raise TransferError("В пакете нет разделов")
    for name, data in sections.items():
        if name not in SECTIONS:
            raise TransferError(f"Неизвестный раздел в пакете: {name}")
        if name == "topology":
            ok = (isinstance(data, dict) and isinstance(data.get("devices"), list)
                  and isinstance(data.get("links"), list))
        else:
            ok = isinstance(data, list)
        if not ok or not all(isinstance(i, dict) for i in _flat_items(name, data)):
            raise TransferError(f"Раздел «{SECTIONS[name]}» повреждён")
    if not isinstance(package.get("meta", {}), dict):
        raise TransferError("Служебные данные пакета повреждены")
    return package


# ---------- Идентичность объектов (по именам) ----------

def _flat_items(section: str, data) -> list:
    return data["devices"] + data["links"] if section == "topology" else data


def _link_id(k: dict) -> str:
    return f"{k.get('a', '')} ({k.get('a_port', '')}) — {k.get('b', '')} ({k.get('b_port', '')})"


def _entries(section: str, data) -> list[tuple[str, str, dict]]:
    """(вид, имя для показа, объект) — по одному на каждый переносимый объект раздела."""
    try:
        if section == "settings":
            return [("настройка", i["key"], i) for i in data]
        if section == "groups":
            return [("группа", i["name"], i) for i in data]
        if section == "users":
            return [("пользователь", i["username"], i) for i in data]
        if section in ("servers", "nodes", "alerts"):
            return [({"servers": "сервер", "nodes": "узел", "alerts": "правило"}[section], i["name"], i)
                    for i in data]
        return ([("устройство", i["name"], i) for i in data["devices"]]
                + [("связь", _link_id(i), i) for i in data["links"]])
    except (KeyError, TypeError):
        raise TransferError(f"Раздел «{SECTIONS[section]}» повреждён") from None


def _status(item: dict, current: dict | None) -> str:
    """new — такого нет; same — совпадает по всем полям пакета; conflict — имя есть, содержимое другое."""
    if current is None:
        return "new"
    return "same" if all(current.get(k) == v for k, v in item.items()) else "conflict"


def _current_index(db: Session, section: str, package_section) -> dict[tuple[str, str], dict]:
    """Объекты этой панели в том же виде, что в пакете: (вид, имя) -> объект."""
    pkg_meta_pw = any("password_hash" in i for i in _flat_items(section, package_section))
    pkg_meta_tok = any("token_hash" in i for i in _flat_items(section, package_section))
    data = _collect(db, section, passwords=pkg_meta_pw, same_tokens=pkg_meta_tok)
    return {(kind, name): item for kind, name, item in _entries(section, data)}


def preview(db: Session, package: dict) -> dict:
    """Что будет с каждым разделом: списки новых / совпадающих / конфликтных объектов и предупреждения."""
    sections, warnings = {}, []
    for name in ORDER:
        if name not in package["sections"]:
            continue
        data = package["sections"][name]
        index = _current_index(db, name, data)
        rows = [(kind, label, _status(item, index.get((kind, label)))) for kind, label, item in _entries(name, data)]
        sections[name] = {
            "title": SECTIONS[name], "rows": rows,
            "new": sum(r[2] == "new" for r in rows), "same": sum(r[2] == "same" for r in rows),
            "conflict": sum(r[2] == "conflict" for r in rows),
        }
    meta = package.get("meta", {})
    if meta.get("passwords"):
        if meta.get("pepper") != _pepper_check():
            warnings.append("Хеши паролей из пакета сделаны с другим PASSWORD_PEPPER: войти по ним здесь не получится, "
                            "пароли придётся задать заново.")
    elif "users" in sections:
        warnings.append("Пароли не перенесены: новым пользователям будет назначен случайный пароль, задайте его "
                        "в Настройки → Пользователи.")
    if "nodes" in sections and not meta.get("same_tokens"):
        warnings.append("Токены узлов не перенесены: для новых узлов будут выданы новые токены (показ один раз).")
    return {"sections": sections, "warnings": warnings, "exported_at": package.get("exported_at", "")}


# ---------- Применение ----------

def _setting_ok(key: str, value) -> bool:
    sdef = REGISTRY.get(key)
    # Для пользовательских настроек глобальная строка — значение по умолчанию для всех
    if sdef is None or key in EXCLUDED_SETTINGS:
        return False
    default = sdef.default
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, type(default))


def _by_name(db: Session, model, field="name") -> dict:
    out = {}
    for obj in db.scalars(select(model).order_by(model.id)):
        out.setdefault(getattr(obj, field), obj)
    return out


def apply(db: Session, package: dict, strategies: dict[str, str], actor: str = "") -> dict:
    """Применяет разделы одной транзакцией: ошибка — откат целиком. strategies: раздел -> skip | replace.

    Возвращает отчёт {"counts": {раздел: {added, replaced, skipped}}, "warnings": [...], "tokens": {узел: токен}}.
    Раздел, которого нет в `strategies`, не применяется.
    """
    report = {"counts": {}, "warnings": [], "tokens": {}}
    meta = package.get("meta", {})
    try:
        for name in ORDER:
            if name not in package["sections"] or name not in strategies:
                continue
            replace = strategies[name] == "replace"
            counts = report["counts"][name] = {"added": 0, "replaced": 0, "skipped": 0}
            _APPLY[name](db, package["sections"][name], replace, counts, report, meta, actor)
            db.flush()
        db.commit()
    except TransferError:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 — любая сбойная запись: откатываем всё, наружу — понятный текст
        db.rollback()
        raise TransferError(f"Импорт отменён, ничего не изменено: {type(exc).__name__}") from exc
    return report


def _apply_settings(db, data, replace, counts, report, meta, actor):
    rows = {r.key: r for r in db.scalars(select(SettingValue).where(SettingValue.user_id.is_(None)))}
    for item in data:
        key, value = item["key"], item.get("value")
        if not _setting_ok(key, value):
            report["warnings"].append(f"Настройка «{key}» пропущена: неизвестна этой панели или другого типа")
            counts["skipped"] += 1
            continue
        row = rows.get(key)
        if row is None:
            db.add(SettingValue(key=key, user_id=None, value=json.dumps(value)))
            counts["added"] += 1
        elif json.loads(row.value) == value:
            continue
        elif replace:
            row.value = json.dumps(value)
            counts["replaced"] += 1
        else:
            counts["skipped"] += 1


def _apply_groups(db, data, replace, counts, report, meta, actor):
    from app.services.permissions import PERMISSIONS

    known = set(PERMISSIONS)
    groups = _by_name(db, Group)
    for item in data:
        perms = sorted(p for p in item.get("permissions", []) if p in known)
        group = groups.get(item["name"])
        if group is None:
            group = Group(name=item["name"][:64], description=str(item.get("description", ""))[:255],
                          builtin=bool(item.get("builtin")))
            group.permissions = [GroupPermission(perm=p) for p in perms]
            db.add(group)
            groups[group.name] = group
            counts["added"] += 1
        elif sorted(p.perm for p in group.permissions) == perms and group.description == item.get("description", ""):
            continue
        elif replace:
            group.description = str(item.get("description", ""))[:255]
            group.permissions = [GroupPermission(perm=p) for p in perms]
            counts["replaced"] += 1
        else:
            counts["skipped"] += 1


def _apply_servers(db, data, replace, counts, report, meta, actor):
    servers = _by_name(db, Server)
    for item in data:
        server = servers.get(item["name"])
        if server is not None and not replace:
            if _status(item, _server_view(server)) != "same":
                counts["skipped"] += 1
            continue
        is_new = server is None
        if is_new:
            server = Server(name=item["name"][:128])
            db.add(server)
            servers[server.name] = server
        server.host = str(item.get("host", ""))[:255]
        server.ssh_port = int(item.get("ssh_port", 22))
        server.collector = item.get("collector", "local")
        server.ssh_user = str(item.get("ssh_user", ""))[:64]
        server.enabled = bool(item.get("enabled", True))
        # Путь к ключу шифруем ключом ЭТОЙ панели
        server.ssh_key_enc = encrypt(str(item.get("ssh_key", "")))
        existing = {s.name: s for s in server.services} if not is_new else {}
        for svc in item.get("services", []):
            row = existing.get(svc["name"])
            if row is None:
                server.services.append(Service(name=svc["name"][:128], check_type=svc.get("check_type", "tcp"),
                                               target=str(svc["target"])[:500]))
            else:
                row.check_type, row.target = svc.get("check_type", "tcp"), str(svc["target"])[:500]
        counts["added" if is_new else "replaced"] += 1


def _server_view(server: Server) -> dict:
    return {"name": server.name, "host": server.host, "ssh_port": server.ssh_port, "collector": server.collector,
            "ssh_user": server.ssh_user, "enabled": server.enabled, "ssh_key": decrypt(server.ssh_key_enc),
            "services": [{"name": v.name, "check_type": v.check_type, "target": v.target}
                         for v in sorted(server.services, key=lambda v: v.name)]}


def _apply_users(db, data, replace, counts, report, meta, actor):
    users = _by_name(db, User, "username")
    groups = _by_name(db, Group)
    for item in data:
        wanted = []
        for gname in item.get("groups", []):
            if gname in groups:
                wanted.append(groups[gname])
            else:
                report["warnings"].append(f"У пользователя «{item['username']}» пропущена группа «{gname}»: "
                                          "её нет в этой панели")
        user = users.get(item["username"])
        if user is None:
            # Нет хеша в пакете — случайный пароль, который никто не знает: администратор задаст его в «Пользователях»
            user = User(username=item["username"][:64], display_name=str(item.get("display_name", ""))[:128],
                        is_active=bool(item.get("is_active", True)), groups=wanted,
                        password_hash=item.get("password_hash") or hash_password(new_token()))
            db.add(user)
            users[user.username] = user
            counts["added"] += 1
        elif replace:
            user.display_name = str(item.get("display_name", ""))[:128]
            # Вошедшего администратора импортом не отключаем: иначе можно запереть себя
            user.is_active = bool(item.get("is_active", True)) or user.username == actor
            user.groups = wanted
            if item.get("password_hash"):
                user.password_hash = item["password_hash"]
            counts["replaced"] += 1
        else:
            counts["skipped"] += 1


def _apply_topology(db, data, replace, counts, report, meta, actor):
    servers = _by_name(db, Server)
    devices = _by_name(db, Device)
    for item in data["devices"]:
        device = devices.get(item["name"])
        if device is not None and not replace:
            counts["skipped"] += 1
            continue
        is_new = device is None
        if is_new:
            device = Device(name=item["name"][:128])
            db.add(device)
            devices[device.name] = device
        device.type = item.get("type", "other")
        device.ip = str(item.get("ip", ""))[:64]
        device.description = str(item.get("description", ""))[:1000]
        device.x, device.y = item.get("x"), item.get("y")
        device.server = servers.get(item.get("server", ""))
        counts["added" if is_new else "replaced"] += 1
    db.flush()
    existing = {(k.a.name, k.a_port, k.b.name, k.b_port): k for k in db.scalars(select(Link))}
    for item in data["links"]:
        a, b = devices.get(item["a"]), devices.get(item["b"])
        if a is None or b is None:
            report["warnings"].append(f"Связь {_link_id(item)} пропущена: устройства нет в этой панели")
            counts["skipped"] += 1
            continue
        link = existing.get((a.name, item.get("a_port", ""), b.name, item.get("b_port", "")))
        if link is None:
            db.add(Link(a_id=a.id, a_port=item.get("a_port", ""), b_id=b.id, b_port=item.get("b_port", ""),
                        label=str(item.get("label", ""))[:128]))
            counts["added"] += 1
        elif link.label != item.get("label", ""):
            if replace:
                link.label = str(item.get("label", ""))[:128]
                counts["replaced"] += 1
            else:
                counts["skipped"] += 1


def _apply_nodes(db, data, replace, counts, report, meta, actor):
    servers = _by_name(db, Server)
    nodes = _by_name(db, Node)
    for item in data:
        name = item["name"]
        if not agents.valid_name(name):
            report["warnings"].append(f"Узел «{name}» пропущен: недопустимое имя")
            counts["skipped"] += 1
            continue
        node = nodes.get(name)
        if node is None:
            node = Node(name=name, token_hash="", server=servers.get(item.get("server", "")))
            if item.get("token_hash"):
                node.token_hash = item["token_hash"]
            else:
                report["tokens"][name] = new_token()
                node.token_hash = hash_token(report["tokens"][name])
            db.add(node)
            nodes[name] = node
            counts["added"] += 1
        elif replace:
            node.server = servers.get(item.get("server", ""))
            if item.get("token_hash"):
                node.token_hash = item["token_hash"]
            counts["replaced"] += 1
        else:
            counts["skipped"] += 1


def _apply_alerts(db, data, replace, counts, report, meta, actor):
    rules = _by_name(db, AlertRule)
    for item in data:
        if item.get("kind") not in alerts.KINDS or item.get("op", ">=") not in alerts.OPS:
            report["warnings"].append(f"Правило «{item.get('name')}» пропущено: неизвестный вид или сравнение")
            counts["skipped"] += 1
            continue
        values = {f: item[f] for f in RULE_FIELDS if f in item}
        rule = rules.get(item["name"])
        if rule is None:
            rule = AlertRule(**values)
            db.add(rule)
            rules[item["name"]] = rule
            counts["added"] += 1
        elif all(getattr(rule, f) == v for f, v in values.items()):
            continue
        elif replace:
            for f, v in values.items():
                setattr(rule, f, v)
            counts["replaced"] += 1
        else:
            counts["skipped"] += 1


_APPLY = {"settings": _apply_settings, "groups": _apply_groups, "servers": _apply_servers, "users": _apply_users,
          "topology": _apply_topology, "nodes": _apply_nodes, "alerts": _apply_alerts}


def file_name() -> str:
    return f"homelab-config-{datetime.now().strftime('%Y%m%d-%H%M')}{FILE_EXT}"
