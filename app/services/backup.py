"""Бэкап БД: горячая копия, проверка, проверка восстановления, шифрование age, ротация, журнал, расписание.

Ядро (`make_backup`) не знает про веб и журнал — его вызывает и CLI (`python -m app.cli backup`), и фоновая
задача. Проверка восстановления идёт ДО шифрования age: у панели нет секретного ключа age, готовый .age она
расшифровать не может, а открытая копия на этот момент ещё лежит во временном файле.
"""
import logging
import os
import shutil
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import migrate
from app.config import config
from app.db import SessionLocal, connect_raw, make_engine, sqlite_path
from app.models import BackupLog, Notification, User
from app.services import audit
from app.services.settings_registry import get_setting
from app.utils import utcnow

log = logging.getLogger("backup")

VERIFY_EVERY = timedelta(days=7)
RETRY_AFTER_FAIL = timedelta(hours=1)  # неудачный плановый бэкап повторяем не чаще раза в час
# Таблицы, чьё число строк сверяем с рабочей БД (данные пользователя, а не метрики и журналы)
KEY_TABLES = ("users", "groups", "servers", "services", "nodes", "boards", "issues", "spaces", "pages",
              "page_versions", "events", "devices", "links", "setting_values", "vpn_subscriptions")


@dataclass
class VerifyResult:
    ok: bool
    detail: str


@dataclass
class BackupResult:
    ok: bool
    path: Path | None = None
    size: int = 0
    ts: datetime | None = None
    error: str = ""
    deleted: int = 0
    encrypted_age: bool = False
    verify: VerifyResult | None = None


def _create_private(path: Path) -> None:
    # Файл создаётся сразу с правами 0600: в копии те же хеши паролей
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))


def _wipe(path: Path) -> None:
    """Перезапись нулями перед удалением. На SSD/CoW-ФС не гарантирует затирание — для этого шифрование диска."""
    if path.exists():
        size = path.stat().st_size
        with open(path, "r+b") as f:
            f.write(b"\0" * size)
            f.flush()
            os.fsync(f.fileno())
        path.unlink()


def backup_dir() -> Path | None:
    """Каталог бэкапов из BACKUP_DIR; None — не задан (по умолчанию бэкапов нет, только вручную из CLI)."""
    return Path(config.backup_dir) if config.backup_dir else None


def _counts(path: Path, tables) -> dict[str, int | None]:
    """Число строк; None — таблицы нет."""
    out: dict[str, int | None] = {}
    with closing(connect_raw(path)) as c:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in tables:
            if t not in have or t not in KEY_TABLES:
                out[t] = None
                continue
            # Имя таблицы параметром не передать; оно только из константы KEY_TABLES и есть в sqlite_master
            out[t] = c.execute("SELECT count(*) FROM " + t).fetchone()[0]  # nosec B608 — имя из KEY_TABLES
    return out


def verify_copy(copy: Path, src: Path, workdir_parent: Path | None = None) -> VerifyResult:
    """Можно ли из этой (открытой) копии восстановиться: открывается, цела, строки совпадают с рабочей БД,
    миграции поднимаются. Работает над дубликатом во временном каталоге — сама копия не меняется."""
    work = Path(tempfile.mkdtemp(prefix="verify-", dir=workdir_parent))  # 0700
    check = work / "check.db"
    try:
        shutil.copyfile(copy, check)
        os.chmod(check, 0o600)
        try:
            with closing(connect_raw(check)) as c:
                res = c.execute("PRAGMA integrity_check").fetchone()[0]
            if res != "ok":
                return VerifyResult(False, f"integrity_check: {res[:200]}")
            got, want = _counts(check, KEY_TABLES), _counts(src, KEY_TABLES)
        except Exception as e:  # неверный ключ SQLCipher, битый файл
            return VerifyResult(False, f"копия не открывается: {type(e).__name__}: {str(e)[:200]}")
        diff = [f"{t}: в копии {got[t]}, в БД {want[t]}" for t in KEY_TABLES
                if want[t] is not None and got[t] != want[t]]
        if diff:
            return VerifyResult(False, "число строк не совпало — " + "; ".join(diff[:5]))
        try:
            migrate.upgrade(f"sqlite:///{check}")
            eng = make_engine(f"sqlite:///{check}")
            try:
                with eng.connect() as conn:
                    conn.exec_driver_sql("SELECT count(*) FROM users").scalar()
            finally:
                eng.dispose()
        except Exception as e:
            return VerifyResult(False, f"миграции на копии не прошли: {type(e).__name__}: {str(e)[:200]}")
        rows = sum(v for v in got.values() if v)
        return VerifyResult(True, f"цела, строк в ключевых таблицах {rows} (совпали с БД), миграции применяются")
    finally:
        for f in work.iterdir():
            _wipe(f)
        work.rmdir()


def make_backup(src: Path | None, out_dir: Path, keep: int, age_recipient: str = "",
                verify: bool = False) -> BackupResult:
    """Горячая копия через backup API (целостна при работающем сервере), проверка, [проверка восстановления],
    [шифрование age], ротация."""
    if not src or not src.exists():
        return BackupResult(False, error=f"БД не найдена: {config.database_url}")
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now()
    dest = out_dir / f"homelab-{now:%Y%m%d-%H%M%S}.db"
    tmp = dest.with_suffix(".tmp")
    _create_private(tmp)
    try:
        with closing(connect_raw(src)) as s, closing(connect_raw(tmp)) as d:
            s.backup(d)
            ok = d.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            d.execute("PRAGMA journal_mode=DELETE")  # копия — один самодостаточный файл без -wal
        if not ok:
            tmp.unlink()
            return BackupResult(False, error="Копия не прошла integrity_check — не сохранена")
        vres = verify_copy(tmp, src, out_dir) if verify else None
        if age_recipient:
            import pyrage

            dest = dest.with_name(dest.name + ".age")
            _create_private(dest)
            dest.write_bytes(pyrage.encrypt(tmp.read_bytes(), [pyrage.x25519.Recipient.from_str(age_recipient)]))
            _wipe(tmp)
        else:
            tmp.rename(dest)
    except Exception:
        for f in (tmp, dest):
            if f.exists():
                _wipe(f)
        raise
    # По времени, а не по имени: посторонний homelab-*.db не должен «обогнать» свежую копию; её саму не трогаем
    others = sorted((f for f in out_dir.glob("homelab-*.db*") if f != dest), key=lambda f: f.stat().st_mtime)
    old = others[:max(0, len(others) - (keep - 1))] if keep > 0 else []
    for f in old:
        f.unlink()
    return BackupResult(True, dest, dest.stat().st_size, now, deleted=len(old), encrypted_age=bool(age_recipient),
                        verify=vres)


# ---------- журнал, уведомления, расписание ----------

def _alert(db: Session, title: str, body: str, kind: str) -> None:
    """Неудача — в колокольчик всем с settings.manage и в журнал безопасности."""
    for u in db.scalars(select(User).where(User.is_active.is_(True))):
        if u.can("settings.manage"):
            db.add(Notification(user_id=u.id, title=title, body=body[:1000], link="/settings/backups"))
    audit.log_event(db, kind, ok=False, detail=body[:500], commit=False)
    db.commit()


def run_backup(trigger: str = "auto", verify: bool | None = None) -> BackupResult:
    """Бэкап по настройкам + запись в журнал + уведомление при неудаче. Блокирующий: вызывать через to_thread."""
    out = backup_dir()
    if not out:
        return BackupResult(False, error="Каталог бэкапов не задан (BACKUP_DIR в .env)")
    with SessionLocal() as db:
        keep = get_setting(db, "backups.keep")
        if verify is None:
            verify = verify_due(db)
    try:
        res = make_backup(sqlite_path(config.database_url), out, keep, config.backup_age_recipient, verify)
    except Exception as e:  # диск заполнен, нет прав, неверный ключ age
        log.exception("Бэкап не удался")
        res = BackupResult(False, error=f"{type(e).__name__}: {str(e)[:300]}")
    with SessionLocal() as db:
        v = res.verify
        db.add(BackupLog(kind="backup", trigger=trigger, file=res.path.name if res.path else "", size=res.size,
                         ok=res.ok, error=res.error[:500], verified=v is not None,
                         verify_ok=v.ok if v else None, verify_detail=(v.detail if v else "")[:500]))
        db.commit()
        if not res.ok:
            _alert(db, "Бэкап не удался", res.error, "backup")
        elif v and not v.ok:
            _alert(db, "Проверка восстановления не прошла", f"{res.path.name}: {v.detail}", "backup_verify")
    return res


def run_verify(trigger: str = "manual") -> VerifyResult:
    """Проверка восстановления «по кнопке»: свежая временная копия, не попадающая в каталог бэкапов."""
    out = backup_dir()
    if not out:
        return VerifyResult(False, "Каталог бэкапов не задан (BACKUP_DIR в .env)")
    src = sqlite_path(config.database_url)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="snapshot-", dir=out))
    snap = work / "snapshot.db"
    try:
        _create_private(snap)
        with closing(connect_raw(src)) as s, closing(connect_raw(snap)) as d:
            s.backup(d)
            d.execute("PRAGMA journal_mode=DELETE")
        res = verify_copy(snap, src, out)
    except Exception as e:
        res = VerifyResult(False, f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        for f in work.iterdir():
            _wipe(f)
        work.rmdir()
    with SessionLocal() as db:
        db.add(BackupLog(kind="verify", trigger=trigger, ok=True, verified=True, verify_ok=res.ok,
                         verify_detail=res.detail[:500]))
        db.commit()
        if not res.ok:
            _alert(db, "Проверка восстановления не прошла", res.detail, "backup_verify")
    return res


def last_backup(db: Session, ok: bool | None = None) -> BackupLog | None:
    q = select(BackupLog).where(BackupLog.kind == "backup").order_by(BackupLog.id.desc())
    if ok is not None:
        q = q.where(BackupLog.ok.is_(ok))
    return db.scalar(q.limit(1))


def last_verify(db: Session) -> BackupLog | None:
    return db.scalar(select(BackupLog).where(BackupLog.verified.is_(True)).order_by(BackupLog.id.desc()).limit(1))


def verify_due(db: Session, now: datetime | None = None) -> bool:
    last = last_verify(db)
    return last is None or (now or utcnow()) - last.ts >= VERIFY_EVERY


def enabled(db: Session) -> bool:
    return bool(backup_dir()) and bool(get_setting(db, "backups.enabled"))


def next_run(db: Session, now: datetime | None = None) -> datetime | None:
    """Ближайший плановый запуск (местное время сервера); None — расписание выключено."""
    if not enabled(db):
        return None
    now = now or datetime.now()
    slot = now.replace(hour=get_setting(db, "backups.hour"), minute=0, second=0, microsecond=0)
    return slot if (now < slot or not _done_since(db, slot)) else slot + timedelta(days=1)


def _done_since(db: Session, slot: datetime) -> bool:
    """Был ли удачный бэкап после момента slot (местное время)."""
    border = slot.astimezone(UTC).replace(tzinfo=None)  # наивное местное время → UTC
    last = last_backup(db, ok=True)
    return bool(last and last.ts >= border)


def due(db: Session, now: datetime | None = None) -> bool:
    """Пора ли делать плановый бэкап: включено, час наступил, сегодня ещё не было, после сбоя — не чаще раза в час."""
    if not enabled(db):
        return False
    now = now or datetime.now()
    slot = now.replace(hour=get_setting(db, "backups.hour"), minute=0, second=0, microsecond=0)
    if now < slot or _done_since(db, slot):
        return False
    failed = last_backup(db, ok=False)
    if failed and (utcnow() - failed.ts) < RETRY_AFTER_FAIL:
        last_ok = last_backup(db, ok=True)
        if not last_ok or failed.ts > last_ok.ts:
            return False
    return True
