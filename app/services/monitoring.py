"""Сбор метрик серверов и проверка сервисов.

local — psutil на своём хосте; ssh — одна команда только для чтения и разбор её вывода.
Сетевые функции (`ssh_banner`, `run_ssh_command`, `check_tcp`, `check_http`) вынесены отдельно,
чтобы в тестах подменять их фикстурами без реальных подключений.
"""
import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import timedelta

import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Server, ServerMetric, Service
from app.utils import utcnow

SSH_COMMAND = "df -B1 -x tmpfs -x devtmpfs -x squashfs; free -b"
TIMEOUT = 8
HISTORY = timedelta(hours=24)
SKIP_FSTYPES = {"tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "devpts", "cgroup2"}


@dataclass
class Metrics:
    ram_total: int | None = None
    ram_used: int | None = None
    ram_free: int | None = None
    disks: list[dict] = field(default_factory=list)


# ---------- Разбор вывода df / free ----------

def parse_df(text: str) -> list[dict]:
    """Разбирает `df -B1`. Длинное имя ФС df переносит на отдельную строку — склеиваем."""
    disks, pending = [], ""
    for line in text.splitlines():
        if not line.strip() or line.startswith("Filesystem") or line.startswith("Файл"):
            continue
        parts = (pending + " " + line).split() if pending else line.split()
        if len(parts) < 6:
            pending = line.strip()
            continue
        pending = ""
        try:
            size, used, free = int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            continue
        disks.append({"mount": " ".join(parts[5:]), "size": size, "used": used, "free": free})
    return disks


def parse_free(text: str) -> tuple[int, int, int] | None:
    """Разбирает `free -b`: (всего, занято, свободно). Свободно = available (с учётом кэша)."""
    header = None
    for line in text.splitlines():
        cols = line.split()
        if cols and cols[0] == "total":
            header = cols
        elif cols and cols[0].rstrip(":") in ("Mem", "Память"):
            values = [int(v) for v in cols[1:]]
            total = values[0]
            if header and "available" in header:
                free = values[header.index("available")]
            else:
                free = values[2]
            return total, total - free, free
    return None


def parse_ssh_output(text: str) -> Metrics:
    # Вывод df идёт до заголовка free (строка, начинающаяся с пробелов и "total")
    df_lines, free_lines, in_free = [], [], False
    for line in text.splitlines():
        if not in_free and line.split()[:1] == ["total"]:
            in_free = True
        (free_lines if in_free else df_lines).append(line)
    ram = parse_free("\n".join(free_lines))
    m = Metrics(disks=parse_df("\n".join(df_lines)))
    if ram:
        m.ram_total, m.ram_used, m.ram_free = ram
    return m


# ---------- Сбор ----------

def in_container() -> bool:
    """В контейнере psutil видит точки монтирования контейнера (/data, /data/backups), а не разделы хоста."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def collect_local() -> Metrics:
    vm = psutil.virtual_memory()
    disks, seen = [], set()
    if in_container():
        return Metrics(vm.total, vm.total - vm.available, vm.available, [])
    for part in psutil.disk_partitions(all=False):
        if part.fstype in SKIP_FSTYPES or part.device in seen or part.mountpoint.startswith("/snap"):
            continue
        # В Docker отдельными файлами монтируются /etc/hosts, /etc/resolv.conf, /etc/localtime — это не разделы
        if os.path.isfile(part.mountpoint):
            continue
        seen.add(part.device)
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        disks.append({"mount": part.mountpoint, "size": usage.total, "used": usage.used, "free": usage.free})
    return Metrics(vm.total, vm.total - vm.available, vm.available, disks)


async def ssh_banner(host: str, port: int) -> tuple[bool, str]:
    """TCP-подключение и чтение SSH-баннера (без аутентификации)."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TIMEOUT)
    except (OSError, asyncio.TimeoutError) as e:
        return False, f"нет TCP-подключения: {e.__class__.__name__}"
    try:
        line = await asyncio.wait_for(reader.readline(), TIMEOUT)
        banner = line.decode(errors="replace").strip()
        if banner.startswith("SSH-"):
            return True, banner[:100]
        # Чужой баннер не показываем: иначе «Серверы» — сканер версий ПО во внутренней сети (аудит A-01)
        return False, "порт открыт, но это не SSH"
    except (OSError, asyncio.TimeoutError):
        return False, "нет SSH-баннера"
    finally:
        writer.close()


async def run_ssh_command(server: Server) -> str:
    from app.services import ssh_access

    # Ключ хоста — запомненный при первом подключении (accept-new), как у консоли; сменился — ошибка проверки
    target = ssh_access.resolve(server, None)
    conn, new_key = await ssh_access.connect(target)
    if new_key:
        ssh_access.remember_key(server, target.host, target.port, new_key)  # сохранит commit в check_server
    async with conn:
        result = await asyncio.wait_for(conn.run(SSH_COMMAND, check=False), TIMEOUT)
        return str(result.stdout)


async def check_tcp(target: str) -> tuple[bool, str]:
    host, _, port = target.rpartition(":")
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), int(port)), TIMEOUT)
        writer.close()
        return True, ""
    except (OSError, ValueError, asyncio.TimeoutError) as e:
        return False, e.__class__.__name__


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # 3xx приходит как HTTPError с кодом — см. обработку ниже


_NO_REDIRECTS = urllib.request.build_opener(_NoRedirect)


def _http_get(url: str) -> tuple[bool, str]:
    if not url.startswith(("http://", "https://")):
        return False, "нужен http:// или https://"
    try:
        # Схема проверена выше (только http/https); по редиректам не идём — ответ 3xx и есть «сервис жив»,
        # а переход мог бы увести проверку на другой внутренний адрес (аудит A-01)
        with _NO_REDIRECTS.open(urllib.request.Request(url, method="GET"), timeout=TIMEOUT) as r:  # nosec B310
            return r.status < 500, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return e.code < 500, f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        return False, e.__class__.__name__


async def check_http(url: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_http_get, url)


# ---------- Проверки с записью в БД ----------

async def check_server(db: Session, server: Server) -> ServerMetric:
    error = ""
    metrics = Metrics()
    if server.collector == "agent":
        return _from_agent(db, server)
    ssh_ok, banner = await ssh_banner(server.host, server.ssh_port)
    if server.collector == "local":
        metrics = await asyncio.to_thread(collect_local)
        online = True  # это наш собственный хост
        if in_container():
            error = "Дашборд в контейнере: разделы хоста не видны — для дисков установите агента и выберите сбор «agent»"
    else:
        online = ssh_ok
        if not ssh_ok:
            error = banner
        else:
            try:
                metrics = parse_ssh_output(await run_ssh_command(server))
            except Exception as e:  # ошибки asyncssh разнообразны; показываем понятную причину без подробностей
                from app.services.ssh_access import error_text

                error = f"SSH-сбор не удался: {error_text(e)}"
    server.online, server.ssh_ok, server.last_checked, server.last_error = online, ssh_ok, utcnow(), error
    metric = ServerMetric(server_id=server.id, online=online, ssh_ok=ssh_ok, ram_total=metrics.ram_total,
                          ram_used=metrics.ram_used, ram_free=metrics.ram_free, disks=json.dumps(metrics.disks))
    db.add(metric)
    db.commit()
    return metric


def _from_agent(db: Session, server: Server) -> ServerMetric:
    """Сбор «agent»: последние значения узла, привязанного к серверу. Ничего не придумываем —
    нет свежих данных агента, значит нет RAM и дисков."""
    from app.models import Node
    from app.services import agents

    node = db.scalar(select(Node).where(Node.server_id == server.id).limit(1))
    online = bool(node and agents.is_online(db, node))
    metrics = agents.server_metrics(node.values, node.sensors) if online else agents.NodeMetrics()
    if not node:
        error = "К серверу не привязан узел с агентом (раздел «Узлы»)"
    else:
        error = "" if online else "Агент не на связи"
    server.online, server.ssh_ok, server.last_checked, server.last_error = online, None, utcnow(), error
    metric = ServerMetric(server_id=server.id, online=online, ssh_ok=None, ram_total=metrics.ram_total,
                          ram_used=metrics.ram_used, ram_free=metrics.ram_free, disks=json.dumps(metrics.disks))
    db.add(metric)
    db.commit()
    return metric


async def check_service(db: Session, service: Service) -> None:
    fn = check_http if service.check_type == "http" else check_tcp
    service.ok, service.last_error = await fn(service.target)
    if service.ok:
        service.last_error = ""
    service.last_checked = utcnow()
    db.commit()


async def run_all_checks(db: Session) -> None:
    for server in db.scalars(select(Server).where(Server.enabled.is_(True))).all():
        await check_server(db, server)
        for service in server.services:
            await check_service(db, service)
    # Старую историю убирает задача «retention» (infra.cleanup): хранится history.retention_days


# ---------- Чтение для UI ----------

def latest_metric(db: Session, server_id: int) -> ServerMetric | None:
    return db.scalar(select(ServerMetric).where(ServerMetric.server_id == server_id)
                     .order_by(ServerMetric.id.desc()).limit(1))


def server_summary(db: Session, server: Server) -> dict:
    """Публичное представление сервера. Пути к ключу здесь нет и быть не должно."""
    m = latest_metric(db, server.id)
    disks = json.loads(m.disks) if m else []
    return {
        "id": server.id, "name": server.name, "host": server.host, "ssh_port": server.ssh_port,
        "collector": server.collector, "ssh_user": server.ssh_user, "has_key": server.has_key,
        "enabled": server.enabled, "online": server.online, "ssh_ok": server.ssh_ok,
        "last_checked": server.last_checked.isoformat() + "Z" if server.last_checked else None,
        "last_error": server.last_error,
        "ram": {"total": m.ram_total, "used": m.ram_used, "free": m.ram_free} if m and m.ram_total else None,
        "disks": disks,
        "services": [{"id": s.id, "name": s.name, "check_type": s.check_type, "target": s.target, "ok": s.ok}
                     for s in server.services],
    }


def history(db: Session, server_id: int) -> list[dict]:
    rows = db.scalars(select(ServerMetric).where(ServerMetric.server_id == server_id,
                                                 ServerMetric.ts >= utcnow() - HISTORY)
                      .order_by(ServerMetric.ts)).all()
    out = []
    for r in rows:
        disks = json.loads(r.disks)
        size = sum(d["size"] for d in disks)
        out.append({
            "ts": r.ts.isoformat() + "Z", "online": r.online,
            "ram_pct": round(r.ram_used * 100 / r.ram_total, 1) if r.ram_total else None,
            "disk_pct": round(sum(d["used"] for d in disks) * 100 / size, 1) if size else None,
        })
    return out
