"""Регистрация фоновых задач в планировщике. Импортируется из lifespan."""
import asyncio

from app.db import SessionLocal, checkpoint_wal
from app.services import agent_install, agent_updates, agents, alerts, audit, backup, calendar, infra, maintenance, monitoring, recording
from app.services.scheduler import job
from app.services.settings_registry import get_setting


def _server_interval() -> int:
    with SessionLocal() as db:
        return int(get_setting(db, "servers.check_interval"))


@job("servers", _server_interval)
async def server_checks() -> None:
    with SessionLocal() as db:
        await monitoring.run_all_checks(db)


@job("agents", lambda: 30)
async def agents_housekeeping() -> None:
    with SessionLocal() as db:
        agents.housekeeping(db)
        agent_updates.expire(db)  # обновления без подтверждения → «не подтвердилось»
        await agents.broadcast_summary(db)


@job("alerts", lambda: 30)
async def alerts_check() -> None:
    with SessionLocal() as db:
        await alerts.run(db)


@job("retention", lambda: 3600)
async def retention() -> None:
    """История и архив — history.retention_days, журнал безопасности — security.log_days."""
    with SessionLocal() as db:
        infra.cleanup(db)
        audit.cleanup(db)
        recording.cleanup(db)
        agent_install.cleanup(db)


@job("reminders", lambda: 30)
async def reminders() -> None:
    with SessionLocal() as db:
        calendar.send_reminders(db)


@job("maintenance", lambda: 15)
async def maintenance_check() -> None:
    """Итог обновления панели от хост-службы → выключить режим обслуживания."""
    with SessionLocal() as db:
        maintenance.check(db)


@job("wal_checkpoint", lambda: 3600)
async def wal_checkpoint() -> None:
    checkpoint_wal()


@job("backup", lambda: 300)
async def scheduled_backup() -> None:
    """Раз в сутки в заданный час; повторов в тот же день нет (см. backup.due). Копирование — в потоке."""
    with SessionLocal() as db:
        if not backup.due(db):
            return
    await asyncio.to_thread(backup.run_backup, "auto")
