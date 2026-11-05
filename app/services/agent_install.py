"""Автоустановка агента: одноразовый код → bootstrap-скрипт → архив → токен → отчёты о шагах; запуск по SSH.

Код — `secrets.token_urlsafe(32)`, живёт 30 минут, в БД только SHA-256; он единственная «авторизация» адресов
`/agent/install/{code}/…` (cookie нет, потому и CSRF для них не нужен). Токен узла выпускается один раз на код.
Панель ничего не исполняет у себя: SSH-ветка только запускает на машине ту же команду, что владелец вставил бы руками.
"""
import asyncio
import hmac
import json
import logging
import re
import secrets
import shlex
from urllib.parse import quote
from datetime import timedelta
from pathlib import Path

from jinja2 import Environment
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import AgentInstall, AgentRelease, Node
from app.services import agent_updates, agents, ssh_access
from app.services.security import hash_token
from app.utils import utcnow

log = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=30)
MAX_REPORT = 16 * 1024
MAX_LOG = 16 * 1024
MAX_STEPS = 40
SSH_TIMEOUT = 600  # секунд на всю установку по SSH
STEPS = {"download", "python", "code", "token", "config", "vpn", "service", "connect", "done"}
# Порядок шагов для полосы прогресса (как их проходят скрипт загрузки и start-agent)
ORDER = ("download", "python", "code", "token", "config", "vpn", "service", "connect")
STATUSES = {"running", "ok", "warn", "failed"}
# Ключи — шаги из контракта start-agent; подсказка показывается под ошибкой
HINTS = {
    "download": "Проверьте, что машина открывает адрес панели (curl -fsS <адрес панели>/ с неё) и что установлены curl, tar, sha256sum.",
    "python": "Нужен Python 3.9+ с модулем venv: поставьте пакет python3-venv (Debian/Ubuntu) и повторите.",
    "code": "Архив агента не распаковался или не прошёл проверку: повторите установку, при повторе — обновите панель.",
    "token": "Токен узла не найден: выпустите новую команду установки (код действует один раз).",
    "config": "Не удалось записать настройки агента: проверьте место на диске и права на /opt/homelab-agent.",
    "vpn": "Не удалось поставить VPN-компонент (нужна сеть до github.com): установите агента без галочки VPN и включите VPN позже.",
    "service": "Служба не создалась: нужен systemd и root; журнал — journalctl -u homelab-agent.",
    "connect": "Агент запущен, но не подключился: проверьте имя узла, токен и что адрес панели доступен с этой машины.",
    "done": "Посмотрите журнал ниже и установите вручную по разделу «Установка вручную» в руководстве.",
}

_CTRL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")

_env = Environment(autoescape=False, keep_trailing_newline=True)  # nosec B701 — это shell, не HTML: значения идут через q
_env.filters["q"] = shlex.quote
# PowerShell: строка в одинарных кавычках, внутри ' удваивается — подстановок $… и `… в ней нет
_env.filters["ps"] = lambda s: "'" + str(s).replace("'", "''") + "'"
_TEMPLATE_WIN = Path(__file__).resolve().parents[1] / "templates" / "agent_install.ps1.j2"
_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "agent_install.sh.j2"


def clean(text: object, limit: int) -> str:
    """Без управляющих символов и ANSI-последовательностей (кроме перевода строки) и не длиннее limit."""
    return _CTRL.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n"))[:limit]


def create(db: Session, node: Node, user_id: int | None, vpn: bool, method: str,
           own_python: bool = False) -> tuple[AgentInstall, str]:
    """Новый код; прежние неиспользованные коды узла гасятся. Сам код возвращается один раз — в БД его нет."""
    now = utcnow()
    for old in db.scalars(select(AgentInstall).where(AgentInstall.node_id == node.id, AgentInstall.expires_at > now,
                                                     AgentInstall.token_issued_at.is_(None))):
        old.expires_at = now
    code = secrets.token_urlsafe(32)
    inst = AgentInstall(node_id=node.id, code_hash=hash_token(code), vpn=vpn, own_python=own_python, created_by=user_id, method=method, created_at=now,
                        expires_at=now + CODE_TTL)
    db.add(inst)
    db.commit()
    return inst, code


def by_code(db: Session, code: str) -> AgentInstall | None:
    """Действующая установка по коду; неизвестный и истёкший одинаково дают None (→ 404 без подробностей)."""
    if not code or len(code) > 100:
        return None
    digest = hash_token(code)
    inst = db.scalar(select(AgentInstall).where(AgentInstall.code_hash == digest))
    if not inst or not hmac.compare_digest(inst.code_hash, digest) or inst.expires_at <= utcnow():
        return None
    return inst


def latest_release(db: Session) -> AgentRelease | None:
    return agent_updates.latest(list(db.scalars(select(AgentRelease))))


def public_url(base_url: str) -> str:
    return base_url if base_url.endswith("/") else base_url + "/"


def install_url(base: str, code: str) -> str:
    return f"{public_url(base)}agent/install/{code}"


def command(base: str, code: str) -> str:
    """Что вставить на машине: `curl -fsSL '<url>' | sudo bash`."""
    return f"curl -fsSL {shlex.quote(install_url(base, code))} | sudo bash"


def windows_command(base: str, code: str) -> str:
    """Для PowerShell от имени администратора: скачать скрипт установки и выполнить."""
    return f"irm '{install_url(base, code)}/windows' | iex"


def render_windows(base: str, code: str, node: Node, release: AgentRelease) -> str:
    base = public_url(base)
    url = install_url(base, code)
    return _env.from_string(_TEMPLATE_WIN.read_text(encoding="utf-8")).render(
        node=node.name, panel_ws=base.replace("http", "ws", 1) + "agent/ws", sha256=release.sha256,
        version=release.version, bundle_url=f"{url}/bundle?v={quote(release.version)}", token_url=f"{url}/token",
        report_url=f"{url}/report", help_url=f"{base}nodes")


def render_script(base: str, code: str, node: Node, release: AgentRelease, vpn: bool, own_python: bool = False) -> str:
    base = public_url(base)
    url = install_url(base, code)
    return _env.from_string(_TEMPLATE.read_text(encoding="utf-8")).render(
        node=node.name, panel_ws=base.replace("http", "ws", 1) + "agent/ws", sha256=release.sha256,
        version=release.version, bundle_url=f"{url}/bundle?v={quote(release.version)}", token_url=f"{url}/token",
        report_url=f"{url}/report", help_url=f"{base}nodes", command=command(base, code), vpn=1 if vpn else 0,
        own_python=1 if own_python else 0)


def release_file(release: AgentRelease) -> Path | None:
    path = agent_updates.release_path(release.version)
    return path if path.is_file() else None


def issue_token(db: Session, inst: AgentInstall) -> str | None:
    """Новый токен узла (старый хеш заменяется). Второй раз по тому же коду — None (→ 409)."""
    node = db.get(Node, inst.node_id)
    if not node or inst.token_issued_at is not None:
        return None
    token = agents.issue_token(node)
    inst.token_issued_at = utcnow()
    set_status(inst, "running")
    db.commit()
    return token


def set_status(inst: AgentInstall, status: str) -> None:
    inst.status = status
    if status in ("ok", "failed"):
        inst.finished_at = utcnow()


def _append_step(inst: AgentInstall, step: str, status: str, message: str) -> None:
    steps = inst.steps
    steps.append({"step": step, "status": status, "message": message, "at": utcnow().isoformat(timespec="seconds")})
    inst.steps_json = json.dumps(steps[-MAX_STEPS:], ensure_ascii=False)


def add_report(db: Session, inst: AgentInstall, data: dict) -> None:
    """Отчёт шага от bootstrap/start-agent; всё из него чистится и урезается, наружу только экранированным."""
    step = clean(data.get("step", ""), 16)
    status = clean(data.get("status", ""), 16)
    if step not in STEPS or status not in STATUSES:
        raise ValueError("неизвестный шаг или статус")
    _append_step(inst, step, status, clean(data.get("message", ""), 500))
    if step == "done":
        inst.log = clean(data.get("log", ""), MAX_LOG)
        set_status(inst, "ok" if status == "ok" else "failed")
    elif status == "failed":
        set_status(inst, "failed")
    elif inst.status == "waiting":
        inst.status = "running"
    db.commit()


def _progress(inst: AgentInstall, steps: list[dict]) -> dict:
    """Одна строка вместо журнала: сколько шагов пройдено и последнее сообщение."""
    seen = {s["step"] for s in steps}
    plan = [s for s in ORDER if s in seen or (s == "vpn" and inst.vpn) or (s == "download" and inst.method == "command")
            or s not in ("vpn", "download")]
    last = {s["step"]: s["status"] for s in steps}
    done = len(plan) if inst.status == "ok" else sum(1 for s in plan if last.get(s) in ("ok", "warn"))
    current = steps[-1] if steps else None
    number = plan.index(current["step"]) + 1 if current and current["step"] in plan else done
    return {"total": len(plan), "done": done, "number": min(max(number, 1), len(plan)),
            "percent": round(100 * done / len(plan)), "current": current}


def view(inst: AgentInstall | None) -> dict | None:
    """Данные для фрагмента: шаги, ошибка с подсказкой."""
    if inst is None:
        return None
    steps = inst.steps
    failed = next((s for s in reversed(steps) if s["status"] == "failed" and s["step"] != "done"), None) \
        or next((s for s in reversed(steps) if s["status"] == "failed"), None)
    expired = inst.status == "waiting" and inst.expires_at <= utcnow()
    return {"progress": _progress(inst, steps),"id": inst.id, "status": inst.status, "method": inst.method, "vpn": inst.vpn, "steps": steps,
            "failed": failed, "hint": HINTS.get(failed["step"], HINTS["done"]) if failed else "",
            "log": inst.log, "expired": expired, "active": inst.status in ("waiting", "running") and not expired}


def last_for(db: Session, node: Node | None) -> AgentInstall | None:
    if node is None:
        return None
    return db.scalar(select(AgentInstall).where(AgentInstall.node_id == node.id).order_by(AgentInstall.id.desc()))


def cleanup(db: Session) -> None:
    """Истёкшие коды старше суток удаляются (итог установки уже в журнале безопасности)."""
    db.execute(delete(AgentInstall).where(AgentInstall.expires_at < utcnow() - timedelta(days=1)))
    db.commit()


# ---------- По SSH ----------

def ssh_command_line(url: str, root: bool) -> str:
    """Команда на машине. Пароль sudo — только в stdin (`sudo -S`), в командную строку не попадает."""
    inner = f"curl -fsSL {shlex.quote(url)} | bash"
    if root:
        return f"bash -c {shlex.quote(inner)}"
    return f"sudo -S -p '' bash -c {shlex.quote(inner)}"


def mask(text: str, secret: str) -> str:
    return text.replace(secret, "********") if secret else text


_tasks: set[asyncio.Task] = set()


def spawn(install_id: int, target: ssh_access.Target, url: str, sudo_password: str) -> None:
    """Фоновая задача: запрос не ждёт установку, статус пишется в БД."""
    task = asyncio.create_task(run_ssh(install_id, target, url, sudo_password))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _finish(install_id: int, status: str, text: str, failure: str = "") -> None:
    from app.db import SessionLocal

    with SessionLocal() as db:
        inst = db.get(AgentInstall, install_id)
        if not inst:
            return
        if failure:
            _append_step(inst, "done", "failed", clean(failure, 500))
        if text and not inst.log:
            inst.log = text[-MAX_LOG:]
        if inst.status not in ("ok", "failed"):
            set_status(inst, status)
        db.commit()


async def run_ssh(install_id: int, target: ssh_access.Target, url: str, sudo_password: str) -> None:
    root = target.username == "root"
    secret = sudo_password
    conn = None
    try:
        conn, new_key = await ssh_access.connect(target)
        if new_key:
            _remember_key(install_id, target, new_key)
        result = await asyncio.wait_for(
            conn.run(ssh_command_line(url, root), input=(secret + "\n") if (secret and not root) else None,
                     check=False), SSH_TIMEOUT)
        out = mask(clean((result.stdout or "") + (result.stderr or ""), 10 ** 6), secret)
        code = result.exit_status
        _finish(install_id, "ok" if code == 0 else "failed", out[-MAX_LOG:],
                "" if code == 0 else f"команда на машине завершилась с кодом {code}")
    except asyncio.TimeoutError:
        _finish(install_id, "failed", "", "установка не уложилась в 10 минут")
    except Exception as exc:  # noqa: BLE001 — причины asyncssh разнообразны; наружу только понятный текст
        _finish(install_id, "failed", "", ssh_access.error_text(exc))
    finally:
        target.password = ""
        if conn is not None:
            conn.close()


def _remember_key(install_id: int, target: ssh_access.Target, line: str) -> None:
    from app.db import SessionLocal
    from app.models import Server

    with SessionLocal() as db:
        inst = db.get(AgentInstall, install_id)
        node = db.get(Node, inst.node_id) if inst else None
        server = db.get(Server, node.server_id) if node and node.server_id else None
        if server:
            ssh_access.remember_key(server, target.host, target.port, line)
            db.commit()
