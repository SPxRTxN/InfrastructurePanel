"""Обновление агента по команде панели (`[update] enabled = true`).

Порядок (ничего из скачанного не выполняется до проверки SHA-256 и самотеста):
  1. пакет — только с панели, к которой подключён агент, по токену узла (Bearer), без редиректов;
  2. SHA-256 из сообщения панели обязан совпасть; распаковка без абсолютных путей, `..` и ссылок;
  3. рядом (releases/<версия>/.venv) ставятся зависимости — только готовые колёса (`--only-binary`);
  4. самотест новой версии на том же конфиге: `--check-config` и `--once` (код 0);
  5. только после этого symlink `current` переключается на новую версию, `previous` — на прежнюю;
  6. агент отвечает `update_result` и перезапускается; если новая версия не подключится к панели за
     `confirm_seconds`, сторож (guard.py) вернёт `previous`.
Откат по кнопке — то же сообщение с версией, которая уже лежит в releases/: скачивание пропускается.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, guard

log = logging.getLogger(__name__)

UPDATE_KEYS = {"enabled", "confirm_seconds", "restart_argv"}
DEFAULT_RESTART = ["sudo", "-n", "systemctl", "restart", "homelab-agent.service"]
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,31}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
URL_PATH_RE = re.compile(r"^/agent/releases/[0-9][0-9A-Za-z.+_-]{0,31}\.tar\.gz$")
MAX_ARCHIVE = 50 * 1024 * 1024
MAX_UNPACKED = 150 * 1024 * 1024
MAX_MEMBERS = 5000
KEEP_RELEASES = 4  # текущая, прежняя и ещё пара последних — для отката без скачивания


class UpdateError(Exception):
    """Понятная причина отказа; уходит панели в detail."""


@dataclass
class UpdateConfig:
    enabled: bool = False
    confirm_seconds: int = 120
    restart_argv: list[str] = field(default_factory=lambda: list(DEFAULT_RESTART))


def parse_update(raw: dict | None) -> UpdateConfig:
    raw = dict(raw or {})
    unknown = set(raw) - UPDATE_KEYS
    if unknown:
        raise ValueError(f"[update]: неизвестные ключи: {', '.join(sorted(unknown))}")
    cfg = UpdateConfig(**raw)
    if not isinstance(cfg.enabled, bool):
        raise ValueError("[update]: enabled — true/false")
    if (not isinstance(cfg.confirm_seconds, int) or isinstance(cfg.confirm_seconds, bool)
            or not 30 <= cfg.confirm_seconds <= 3600):
        raise ValueError("[update]: confirm_seconds — целое 30..3600")
    argv = cfg.restart_argv
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 10
            or not all(isinstance(a, str) and a and len(a) <= 200 for a in argv)):
        raise ValueError("[update]: restart_argv — список строк (команда перезапуска без оболочки)")
    return cfg


def find_prefix() -> Path | None:
    """<prefix>, если код запущен из <prefix>/releases/<версия>/ (раскладка install.sh); иначе None."""
    code = Path(__file__).resolve().parent.parent
    return code.parent.parent if code.parent.name == "releases" else None


def check_update(raw: dict | None) -> list[str]:
    """Для `--check-config`: проблемы, из-за которых обновление не заработает."""
    cfg = parse_update(raw)
    if cfg.enabled and find_prefix() is None:
        return ["[update]: агент установлен без раскладки releases/ — выполните packaging/install.sh --upgrade"]
    return []


# ---------- Скачивание ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # Токен узла уходит только на панель; перенаправления не выполняем
        return None


def panel_base(ws_url: str) -> str:
    """wss://host:port/agent/ws → https://host:port (та же панель, к которой подключён агент)."""
    parts = urlsplit(ws_url)
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme)
    if not scheme or not parts.netloc:
        raise UpdateError("не удалось определить адрес панели")
    return f"{scheme}://{parts.netloc}"


def download(url: str, token: str, ctx: ssl.SSLContext | None, dest: Path, max_bytes: int = MAX_ARCHIVE) -> None:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": f"homelab-agent/{__version__}"})
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    try:
        with opener.open(req, timeout=60) as resp, open(dest, "wb") as out:
            if resp.status != 200:
                raise UpdateError(f"панель ответила {resp.status}")
            size = 0
            while chunk := resp.read(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise UpdateError("архив больше допустимого размера")
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"панель ответила {exc.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdateError(f"скачивание не удалось: {getattr(exc, 'reason', exc)}") from None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# ---------- Распаковка ----------

def safe_extract(archive: Path, dest: Path) -> None:
    """Распаковать tar.gz в dest. Отвергаем абсолютные пути, `..`, ссылки и устройства; режимы задаём сами.
    Архив может лежать целиком в одном верхнем каталоге (push-agent: homelab-agent/…) — он отбрасывается."""
    try:
        tf = tarfile.open(archive, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise UpdateError(f"не tar.gz: {exc}") from None
    with tf:
        members = tf.getmembers()
        if len(members) > MAX_MEMBERS:
            raise UpdateError("в архиве слишком много файлов")
        names: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
        total = 0
        for m in members:
            if m.isdev() or m.issym() or m.islnk() or m.isfifo():
                raise UpdateError(f"в архиве недопустимый элемент: {m.name[:80]}")
            parts = tuple(p for p in m.name.split("/") if p not in ("", "."))
            if m.name.startswith("/") or ".." in parts or any("\\" in p or "\0" in p for p in parts):
                raise UpdateError(f"небезопасный путь в архиве: {m.name[:80]}")
            if parts:
                names.append((m, parts))
            total += max(m.size, 0)
        if total > MAX_UNPACKED:
            raise UpdateError("архив слишком большой после распаковки")
        strip = 0
        if ("homelab_agent", "__init__.py") not in {p for _, p in names}:
            roots = {p[0] for _, p in names}
            if len(roots) == 1 and (next(iter(roots)), "homelab_agent", "__init__.py") in {p for _, p in names}:
                strip = 1
            else:
                raise UpdateError("в архиве нет homelab_agent/__init__.py")
        dest.mkdir(parents=True, exist_ok=False)
        for m, parts in names:
            rel = parts[strip:]
            if not rel:
                continue
            target = dest.joinpath(*rel)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            target.chmod(0o755 if m.mode & 0o100 else 0o644)
        for d in [dest, *dest.rglob("*")]:
            if d.is_dir():
                d.chmod(0o755)


# ---------- Команды ----------

def run_cmd(argv: list[str], cwd: Path | None, timeout: int) -> tuple[int, str]:
    """Запуск списком аргументов, без оболочки. Возвращает (код, хвост вывода)."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    try:
        res = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "таймаут"
    except OSError as exc:
        return 127, f"не запустить: {exc.strerror}"
    return res.returncode, ((res.stdout or "") + (res.stderr or "")).strip()[-400:]


class Updater:
    def __init__(self, cfg: UpdateConfig, config_path: str | None, *, prefix: Path | None = None,
                 state: Path | None = None, run=run_cmd, fetch=download, python: str | None = None,
                 current_version: str = __version__, now=time.time):
        self.cfg = cfg
        self.config_path = config_path
        self.prefix = prefix if prefix is not None else find_prefix()
        self.state = state if state is not None else guard.state_dir()
        self.run, self.fetch = run, fetch
        self.python = python or sys.executable
        self.version = current_version
        self.now = now
        self.restart_pending = False
        self._busy = False

    # --- вход от транспорта ---
    async def handle(self, msg: dict, panel) -> dict:
        import asyncio

        uid = msg.get("id") if isinstance(msg.get("id"), str) and len(msg["id"]) <= 64 else ""
        version = msg.get("version") if isinstance(msg.get("version"), str) else ""
        if self._busy:
            return self._result(uid, False, "обновление уже идёт", version)
        self._busy = True
        try:
            detail = await asyncio.to_thread(self.apply, msg, panel)
            return self._result(uid, True, detail, version)
        except UpdateError as exc:
            log.warning("обновление до %s не удалось: %s", version, exc)
            return self._result(uid, False, str(exc), version)
        except Exception as exc:  # noqa: BLE001 — любой сбой не должен ронять агента
            log.exception("обновление: неожиданная ошибка")
            return self._result(uid, False, f"внутренняя ошибка: {type(exc).__name__}", version)
        finally:
            self._busy = False

    @staticmethod
    def _result(uid: str, ok: bool, detail: str, version: str) -> dict:
        return {"type": "update_result", "id": uid, "ok": ok, "detail": detail[:500], "version": version[:32]}

    def restart(self) -> int:
        return self.run(self.cfg.restart_argv, None, 30)[0]

    # --- само обновление (в потоке) ---
    def apply(self, msg: dict, panel) -> str:
        version, sha, url = msg.get("version"), msg.get("sha256"), msg.get("url")
        if not isinstance(version, str) or not VERSION_RE.match(version):
            raise UpdateError("некорректная версия")
        if not isinstance(sha, str) or not SHA_RE.match(sha.lower()):
            raise UpdateError("нет SHA-256 пакета — без него не обновляюсь")
        path = urlsplit(url).path if isinstance(url, str) else ""
        if not URL_PATH_RE.match(path) or path != f"/agent/releases/{version}.tar.gz":
            raise UpdateError("адрес пакета должен быть /agent/releases/<версия>.tar.gz на панели")
        if self.prefix is None or not (self.prefix / "releases").is_dir():
            raise UpdateError("агент установлен без раскладки releases/ (packaging/install.sh --upgrade)")
        if not self.config_path:
            raise UpdateError("неизвестен путь конфига для самотеста")
        if not self.state.is_dir():
            raise UpdateError(f"нет каталога состояния {self.state}: сторож не сможет вернуть версию")
        releases = self.prefix / "releases"
        current = guard._target(self.prefix, "current")
        if current == version:
            return f"версия {version} уже установлена"

        dest = releases / version
        fresh = not (dest / ".verified").exists() or not (dest / ".venv" / "bin" / "python").exists()
        if fresh:
            shutil.rmtree(dest, ignore_errors=True)  # остатки прерванной попытки
            try:
                self._install(panel, version, sha.lower(), url_path=path, dest=dest)
            except BaseException:
                shutil.rmtree(dest, ignore_errors=True)
                raise
        try:
            self._selftest(dest)
        except UpdateError:
            if fresh:
                shutil.rmtree(dest, ignore_errors=True)
            raise
        (dest / ".verified").touch()

        # Переключение: previous → прежняя, current → новая; сторож смотрит на switch.json
        if current:
            guard.replace_symlink(self.prefix / "previous", f"releases/{current}")
        guard.replace_symlink(self.prefix / "current", f"releases/{version}")
        guard.write_switch(self.state, version, current or self.version, self.cfg.confirm_seconds, self.now())
        self.restart_pending = True
        self._prune(releases, keep={version, current})
        return f"версия {version} проверена и включена; перезапуск" + (" (из локального каталога)" if not fresh else "")

    def _install(self, panel, version: str, sha: str, url_path: str, dest: Path) -> None:
        from .transport import ssl_context

        token = panel.read_token()
        tmp = dest.parent / f".dl-{version}.tar.gz"
        try:
            self.fetch(panel_base(panel.url) + url_path, token, ssl_context(panel), tmp)
            if sha256_file(tmp) != sha:
                raise UpdateError("SHA-256 пакета не совпал — пакет отвергнут")
            safe_extract(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        req = dest / ("requirements-display.txt" if (self.prefix / ".display").exists() else "requirements.txt")
        if not req.is_file():
            raise UpdateError(f"в пакете нет {req.name}")
        code, out = self.run([self.python, "-m", "venv", str(dest / ".venv")], dest, 180)
        if code:
            raise UpdateError(f"venv не создан: {out}")
        py = str(dest / ".venv" / "bin" / "python")
        code, out = self.run([py, "-m", "pip", "install", "--quiet", "--only-binary=:all:", "-r", str(req)], dest, 900)
        if code:
            raise UpdateError(f"зависимости не поставились: {out}")

    def _selftest(self, dest: Path) -> None:
        py = str(dest / ".venv" / "bin" / "python")
        for flag in ("--check-config", "--once"):
            code, out = self.run([py, "-m", "homelab_agent", "-c", str(self.config_path), flag], dest, 180)
            if code:
                raise UpdateError(f"самотест новой версии ({flag}) не прошёл, код {code}: {out}")

    def _prune(self, releases: Path, keep: set) -> None:
        previous = guard._target(self.prefix, "previous")
        keep = {k for k in (*keep, previous) if k}
        old = sorted((d for d in releases.iterdir() if d.is_dir() and VERSION_RE.match(d.name) and d.name not in keep),
                     key=lambda d: d.stat().st_mtime, reverse=True)
        for d in old[max(0, KEEP_RELEASES - len(keep)):]:
            shutil.rmtree(d, ignore_errors=True)
