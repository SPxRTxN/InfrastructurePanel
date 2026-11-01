"""Обновление агентов из панели: каталог версий, сообщение `update`, результат, подтверждение по hello.

Протокол — handoff/homelab-agent/docs/Протокол.md («Обновление агента»). Панель не ходит по SSH и ничего не
исполняет на узле: она кладёт архив в каталог версий и просит узел, который сам включил `[update]`, скачать его
(по токену узла), проверить SHA-256, пройти самотест и переключиться.
"""
import gzip
import hashlib
import io
import logging
import os
import re
import tarfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import config
from app.db import sqlite_path
from app.models import AgentRelease, Node, NodeEvent, NodeUpdate, User
from app.services import agents
from app.services.security import hash_token
from app.utils import utcnow

MAX_ARCHIVE = 50 * 1024 * 1024
MAX_UNPACKED = 150 * 1024 * 1024
MAX_MEMBERS = 5000
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,31}$")
VERSION_LINE = re.compile(rb"""^__version__\s*=\s*["']([^"'\r\n]{1,32})["']\s*$""", re.M)
# Агент вернулся с новой версией не позже этого срока после ответа ok; иначе «не подтвердилось»
CONFIRM_TIMEOUT = timedelta(minutes=5)
# Ответа агента нет (скачивание и pip на Raspberry Pi могут идти долго)
RESULT_TIMEOUT = timedelta(minutes=20)
# Старая версия в hello раньше этого срока после ok — ещё не перезапущенный процесс, а не откат
ROLLBACK_AFTER = timedelta(seconds=30)
MAX_BULK = 50
TARGET_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}@[A-Za-z0-9.:_-]{1,255}$")


class ReleaseError(ValueError):
    """Понятная причина отказа для администратора."""


def releases_dir() -> Path:
    """<каталог данных>/agent-releases (рядом с БД); создаётся при первом сохранении."""
    db = sqlite_path(config.database_url)
    return (db.parent if db else Path("data")) / "agent-releases"


def release_path(version: str) -> Path:
    if not VERSION_RE.match(version):
        raise ReleaseError("некорректная версия")
    return releases_dir() / f"{version}.tar.gz"


def version_key(version: str) -> tuple:
    """0.10.0 > 0.9.0: сравнение по числам; нечисловые хвосты не мешают."""
    return tuple(int(p) for p in re.findall(r"\d+", version))


def latest(releases: list[AgentRelease]) -> AgentRelease | None:
    return max(releases, key=lambda r: version_key(r.version), default=None)


# ---------- Архив ----------

def inspect_archive(data: bytes) -> str:
    """Проверить, что это tar.gz с homelab_agent/__init__.py, и вернуть версию из него."""
    if not data:
        raise ReleaseError("файл пуст")
    if len(data) > MAX_ARCHIVE:
        raise ReleaseError(f"архив больше {MAX_ARCHIVE // 1024 // 1024} МБ")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
        members = tf.getmembers()
    except (tarfile.TarError, OSError, EOFError):
        raise ReleaseError("это не tar.gz") from None
    with tf:
        if len(members) > MAX_MEMBERS:
            raise ReleaseError("в архиве слишком много файлов")
        init, total = None, 0
        for m in members:
            parts = [p for p in m.name.split("/") if p not in ("", ".")]
            if m.name.startswith("/") or ".." in parts or m.issym() or m.islnk() or m.isdev() or m.isfifo():
                raise ReleaseError(f"недопустимый элемент архива: {m.name[:80]}")
            total += max(m.size, 0)
            # homelab_agent/__init__.py в корне или внутри единственного верхнего каталога (push-agent)
            if m.isfile() and parts[-2:] == ["homelab_agent", "__init__.py"] and len(parts) <= 3:
                init = m
        if total > MAX_UNPACKED:
            raise ReleaseError("архив слишком большой после распаковки")
        if init is None:
            raise ReleaseError("в архиве нет homelab_agent/__init__.py")
        if init.size > 64 * 1024:
            raise ReleaseError("homelab_agent/__init__.py слишком большой")
        found = VERSION_LINE.search(tf.extractfile(init).read())
    if not found:
        raise ReleaseError("в homelab_agent/__init__.py нет строки __version__ = \"…\"")
    version = found.group(1).decode()
    if not VERSION_RE.match(version):
        raise ReleaseError(f"некорректная версия в архиве: {version[:32]}")
    return version


def add_release(db: Session, data: bytes, claimed: str, notes: str, user: User | None) -> AgentRelease:
    """Сохранить архив в каталог версий. Версию определяет архив; поле формы (если заполнено) должно с ней совпасть."""
    version = inspect_archive(data)
    claimed = claimed.strip()
    if claimed and claimed != version:
        raise ReleaseError(f"версия в форме ({claimed[:32]}) не совпадает с версией в архиве ({version})")
    if db.scalar(select(AgentRelease).where(AgentRelease.version == version)):
        raise ReleaseError(f"версия {version} уже есть в каталоге")
    path = release_path(version)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)  # SHA-256 считает панель по тому, что реально лежит в файле
    release = AgentRelease(version=version, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=len(data),
                           notes=notes.strip()[:500], uploaded_by=user.id if user else None)
    db.add(release)
    db.commit()
    return release


BUNDLE_DIR = Path(__file__).resolve().parents[2] / "agent-bundle"  # в образе: /app/agent-bundle (Dockerfile)
BUNDLE_SKIP = {"__pycache__", ".pytest_cache", ".venv", "tests", "Obsidian"}


def pack_bundle(src: Path) -> bytes:
    """Детерминированный tar.gz каталога агента: сортировка, mtime 0, без владельцев — тот же SHA при той же версии."""
    buf = io.BytesIO()
    # gzip сам кладёт в заголовок текущее время — фиксируем 0, иначе SHA меняется от запуска к запуску
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as tf:
        for path in sorted(src.rglob("*")):
            rel = path.relative_to(src)
            if any(p in BUNDLE_SKIP for p in rel.parts) or not path.is_file() or path.is_symlink():
                continue
            info = tf.gettarinfo(str(path), arcname=str(rel))
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
            with open(path, "rb") as f:
                tf.addfile(info, f)
    return buf.getvalue()


def register_bundled(db: Session, src: Path = BUNDLE_DIR) -> AgentRelease | None:
    """Версия агента, приехавшая вместе с панелью (обновил панель — агенты видят «доступно обновление»).
    Ничего не делает вне образа (нет каталога) и если такая версия уже в каталоге."""
    init = src / "homelab_agent" / "__init__.py"
    if not init.is_file():
        return None
    found = VERSION_LINE.search(init.read_bytes())
    if not found or db.scalar(select(AgentRelease).where(AgentRelease.version == found.group(1).decode())):
        return None
    from app import __version__ as panel_version

    return add_release(db, pack_bundle(src), "", f"встроено в панель {panel_version}", None)


def register_bundled_safely() -> None:
    from app.db import SessionLocal

    try:
        with SessionLocal() as db:
            register_bundled(db)
    except Exception:  # noqa: BLE001 — каталог версий не должен мешать старту панели
        logging.getLogger(__name__).exception("встроенная версия агента не зарегистрирована")


def delete_release(db: Session, release: AgentRelease) -> None:
    release_path(release.version).unlink(missing_ok=True)
    db.delete(release)
    db.commit()


# ---------- Скачивание агентом ----------

def node_by_token(db: Session, header: str | None) -> Node | None:
    """Узел по `Authorization: Bearer <токен>`: сравнение хеша, как в handle_hello."""
    scheme, _, token = (header or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 200:
        return None
    return db.scalar(select(Node).where(Node.token_hash == hash_token(token)))


# ---------- Обновление узла ----------

def supports_update(node: Node) -> bool:
    return "update" in node.info.get("features", [])


def message_for(release: AgentRelease, upd: NodeUpdate) -> dict:
    return {"type": "update", "id": f"u-{upd.id}", "version": release.version,
            "url": f"/agent/releases/{release.version}.tar.gz", "sha256": release.sha256}


async def request_update(db: Session, node: Node, release: AgentRelease, user: User) -> NodeUpdate:
    """Отправить узлу update; отказ (не умеет, офлайн, уже на этой версии) записывается в журнал со статусом failed."""
    upd = NodeUpdate(node_id=node.id, from_version=node.version, to_version=release.version, user_id=user.id)
    db.add(upd)
    if node.version == release.version:
        upd.status, upd.detail = "failed", "уже стоит эта версия"
    elif not supports_update(node):
        upd.status, upd.detail = "failed", "агент не объявил возможность update (нужно [update] enabled в конфиге агента)"
    elif not agents.is_online(db, node) or node.id not in agents.hub.sockets:
        upd.status, upd.detail = "failed", "узел не на связи"
    else:
        db.flush()
        if not await agents.hub.send(node.id, message_for(release, upd)):
            upd.status, upd.detail = "failed", "не удалось отправить"
        else:
            upd.detail = "отправлено, ждём ответа агента"
    db.commit()
    return upd


def on_result(db: Session, node: Node, data: dict) -> None:
    """update_result от агента: ok — версия проверена и переключена (ждём возвращения), иначе отказ."""
    raw = str(data.get("id", "")).removeprefix("u-")
    upd = db.get(NodeUpdate, int(raw)) if raw.isdigit() else None
    if not upd or upd.node_id != node.id or upd.status != "sent":
        return
    detail = str(data.get("detail", ""))[:400]
    now = utcnow()
    if data.get("ok") is True:
        upd.status, upd.detail = "switching", detail or "версия включена, агент перезапускается"
    else:
        upd.status, upd.detail = "failed", detail or "агент отказал"
        db.add(NodeEvent(node_id=node.id, ts=now, level="warning",
                         text=f"Обновление агента до {upd.to_version} не удалось: {detail}"[:500]))
    upd.updated_at = now
    db.commit()


def on_hello(db: Session, node: Node) -> None:
    """Агент подключился (node.version уже из hello): подтвердить обновление или заметить откат."""
    now = utcnow()
    for upd in db.scalars(select(NodeUpdate).where(NodeUpdate.node_id == node.id,
                                                   NodeUpdate.status.in_(("sent", "switching")))):
        if node.version == upd.to_version:
            upd.status, upd.detail, upd.updated_at = "done", f"обновлено: агент вернулся с версией {node.version}", now
        elif upd.status == "switching" and node.version == upd.from_version and now - upd.updated_at > ROLLBACK_AFTER:
            upd.status, upd.updated_at = "rolled_back", now
            upd.detail = "откат: новая версия не поднялась, сторож вернул прежнюю"
            db.add(NodeEvent(node_id=node.id, ts=now, level="warning",
                             text=f"Обновление агента до {upd.to_version}: новая версия не поднялась, откат на {node.version}"))
    db.commit()


def expire(db: Session) -> None:
    """Сроки: нет ответа агента — failed; нет возвращения с новой версией за 5 минут — «не подтвердилось»."""
    now = utcnow()
    for upd in db.scalars(select(NodeUpdate).where(NodeUpdate.status.in_(("sent", "switching")))):
        if upd.status == "switching" and now - upd.updated_at > CONFIRM_TIMEOUT:
            upd.status, upd.detail, upd.updated_at = "unconfirmed", "не подтвердилось: агент не вернулся с новой версией за 5 минут", now
            db.add(NodeEvent(node_id=upd.node_id, ts=now, level="warning",
                             text=f"Обновление агента до {upd.to_version} не подтвердилось"))
        elif upd.status == "sent" and now - upd.requested_at > RESULT_TIMEOUT:
            upd.status, upd.detail, upd.updated_at = "failed", "нет ответа агента", now
    db.commit()


# ---------- Массовое добавление узлов (G3) ----------

def parse_targets(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Строки `имя user@host` → ([(имя, user@host)], ошибки). Пустые строки и # пропускаются."""
    rows, errors = [], []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if len(lines) > MAX_BULK:
        errors.append(f"за раз не больше {MAX_BULK} узлов")
        lines = lines[:MAX_BULK]
    for ln in lines:
        parts = ln.split()
        if len(parts) != 2 or not agents.valid_name(parts[0]) or not TARGET_RE.match(parts[1]):
            errors.append(f"«{ln[:80]}»: нужен вид «имя user@host» (имя — латиница, цифры, . _ -)")
        else:
            rows.append((parts[0], parts[1]))
    return rows, errors


def add_nodes(db: Session, text: str) -> tuple[list[dict], list[str]]:
    """Создать узлы и токены. Токен возвращается один раз — в БД остаётся хеш."""
    rows, errors = parse_targets(text)
    created, seen = [], set()
    for name, target in rows:
        if name in seen or db.scalar(select(Node).where(Node.name == name)):
            errors.append(f"«{name}»: узел с таким именем уже есть")
            continue
        seen.add(name)
        node = Node(name=name)
        token = agents.issue_token(node)
        db.add(node)
        created.append({"name": name, "target": target, "token": token, "node": node})
    db.commit()
    return created, errors
