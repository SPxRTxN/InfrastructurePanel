"""Обновление агента: безопасная распаковка, SHA-256, самотест, переключение symlink, откат сторожем.

Всё на временных каталогах и подменённых командах: ни сети, ни root, ни systemd.
"""
import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from homelab_agent import guard, updater
from homelab_agent.config import PanelConfig, parse_config
from homelab_agent.runner import Agent
from homelab_agent.updater import UpdateError, Updater, parse_update, safe_extract

PANEL = PanelConfig(url="wss://panel.example:8443/agent/ws", token="node-token-secret")


def make_tar(path: Path, files: dict, extra=None) -> str:
    """tar.gz из словаря {имя: байты}; extra — функция, добавляющая особые элементы. Возвращает SHA-256."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if extra:
            extra(tf)
    return hashlib.sha256(path.read_bytes()).hexdigest()


GOOD = {"homelab_agent/__init__.py": b'__version__ = "0.2.0"\n', "requirements.txt": b"psutil==1\n"}


class Env:
    """Раскладка install.sh во временном каталоге + подмены команд и скачивания."""

    def __init__(self, tmp: Path):
        self.prefix, self.state = tmp / "opt", tmp / "state"
        rel = self.prefix / "releases" / "0.1.0"
        (rel / "homelab_agent").mkdir(parents=True)
        (rel / ".venv" / "bin").mkdir(parents=True)
        (rel / ".venv" / "bin" / "python").write_text("")
        (rel / ".verified").touch()
        (self.prefix / "current").symlink_to("releases/0.1.0")
        self.state.mkdir()
        self.archive = tmp / "pkg.tar.gz"
        self.sha = make_tar(self.archive, GOOD)
        self.fetched: list = []
        self.commands: list = []
        self.fail_flag: str | None = None  # на какой флаг самотеста вернуть ошибку
        self.u = Updater(parse_update({"enabled": True}), str(tmp / "agent.toml"), prefix=self.prefix,
                         state=self.state, run=self.run, fetch=self.fetch, python="/usr/bin/python3",
                         current_version="0.1.0", now=lambda: 1000.0)

    def fetch(self, url, token, ctx, dest, *a):
        self.fetched.append((url, token))
        dest.write_bytes(self.archive.read_bytes())

    def run(self, argv, cwd, timeout):
        self.commands.append(argv)
        if self.fail_flag and self.fail_flag in argv:
            return 3, "boom"
        return 0, ""

    def msg(self, version="0.2.0", **kw):
        return {"type": "update", "id": "u-1", "version": version, "sha256": self.sha,
                "url": f"/agent/releases/{version}.tar.gz", **kw}

    def current(self):
        return (self.prefix / "current").resolve().name


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def apply(env, **kw):
    return asyncio.run(env.u.handle(env.msg(**kw), PANEL))


# ---------- распаковка ----------

@pytest.mark.parametrize("bad", ["/etc/passwd", "../evil", "homelab_agent/../../evil", "a/../../b"])
def test_extract_rejects_unsafe_paths(tmp_path, bad):
    make_tar(tmp_path / "a.tar.gz", {**GOOD, bad: b"x"})
    with pytest.raises(UpdateError, match="небезопасный"):
        safe_extract(tmp_path / "a.tar.gz", tmp_path / "out")
    assert not (tmp_path / "evil").exists()


def test_extract_rejects_links_and_devices(tmp_path):
    def link(tf):
        info = tarfile.TarInfo("homelab_agent/x")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tf.addfile(info)
    make_tar(tmp_path / "a.tar.gz", GOOD, extra=link)
    with pytest.raises(UpdateError, match="недопустимый"):
        safe_extract(tmp_path / "a.tar.gz", tmp_path / "out")


def test_extract_requires_package_and_strips_single_root(tmp_path):
    make_tar(tmp_path / "a.tar.gz", {"readme.txt": b"x"})
    with pytest.raises(UpdateError, match="__init__"):
        safe_extract(tmp_path / "a.tar.gz", tmp_path / "o1")
    make_tar(tmp_path / "b.tar.gz", {f"homelab-agent/{k}": v for k, v in GOOD.items()})
    safe_extract(tmp_path / "b.tar.gz", tmp_path / "o2")
    assert (tmp_path / "o2" / "homelab_agent" / "__init__.py").is_file()
    (tmp_path / "bad.tar.gz").write_bytes(b"not a tar")
    with pytest.raises(UpdateError, match="не tar.gz"):
        safe_extract(tmp_path / "bad.tar.gz", tmp_path / "o3")


# ---------- обновление ----------

def test_success_switches_symlinks_after_selftest(env):
    r = apply(env)
    assert r == {"type": "update_result", "id": "u-1", "ok": True, "detail": r["detail"], "version": "0.2.0"}
    assert env.current() == "0.2.0" and (env.prefix / "previous").resolve().name == "0.1.0"
    # Скачано с хоста панели из ws-адреса, токен узла передан
    assert env.fetched == [("https://panel.example:8443/agent/releases/0.2.0.tar.gz", "node-token-secret")]
    flat = [" ".join(c) for c in env.commands]
    assert any("-m venv" in c for c in flat)
    assert any("pip install" in c and "--only-binary=:all:" in c for c in flat)
    assert any(c.endswith("--check-config") for c in flat) and any(c.endswith("--once") for c in flat)
    # Самотест — на новой версии, до переключения; перезапуск ждёт агента
    assert all("releases/0.2.0" in c for c in flat if "homelab_agent" in c)
    sw = json.loads((env.state / "switch.json").read_text())
    assert sw["version"] == "0.2.0" and sw["previous"] == "0.1.0" and sw["confirm_seconds"] == 120
    assert env.u.restart_pending


def test_foreign_host_in_url_is_ignored(env):
    apply(env, url="https://evil.example/agent/releases/0.2.0.tar.gz")
    assert env.fetched[0][0].startswith("https://panel.example:8443/")


@pytest.mark.parametrize("url", ["/etc/passwd", "/agent/releases/../x.tar.gz", "/agent/releases/0.3.0.tar.gz", ""])
def test_bad_url_refused(env, url):
    r = apply(env, url=url)
    assert not r["ok"] and "адрес пакета" in r["detail"] and not env.fetched and env.current() == "0.1.0"


def test_wrong_sha_refused_and_nothing_left(env):
    r = apply(env, sha256="0" * 64)
    assert not r["ok"] and "SHA-256" in r["detail"]
    assert env.current() == "0.1.0" and not (env.prefix / "releases" / "0.2.0").exists()
    assert not env.commands  # ничего из скачанного не запускалось
    assert not apply(env, sha256="")["ok"]


@pytest.mark.parametrize("flag", ["--check-config", "--once"])
def test_failed_selftest_keeps_symlink(env, flag):
    env.fail_flag = flag
    r = apply(env)
    assert not r["ok"] and "самотест" in r["detail"] and flag in r["detail"]
    assert env.current() == "0.1.0" and not (env.prefix / "previous").exists()
    assert not (env.state / "switch.json").exists() and not env.u.restart_pending
    assert not (env.prefix / "releases" / "0.2.0").exists()


def test_failed_pip_keeps_symlink(env):
    env.fail_flag = "--only-binary=:all:"
    r = apply(env)
    assert not r["ok"] and "зависимости" in r["detail"] and env.current() == "0.1.0"


def test_rollback_uses_local_release_without_download(env):
    assert apply(env)["ok"]
    env.fetched.clear()
    r = apply(env, version="0.1.0")
    assert r["ok"] and not env.fetched and env.current() == "0.1.0"
    assert (env.prefix / "previous").resolve().name == "0.2.0"


def test_same_version_and_busy(env):
    r = apply(env, version="0.1.0")
    assert r["ok"] and "уже" in r["detail"] and not env.u.restart_pending
    env.u._busy = True
    assert "уже идёт" in apply(env)["detail"]


def test_needs_state_dir_and_layout(env, tmp_path):
    env.state.rmdir()
    assert "сторож" in apply(env)["detail"]
    env.u.prefix = tmp_path / "nowhere"
    assert "раскладки" in apply(env)["detail"]


def test_prune_keeps_current_previous_and_limit(env):
    for v in ("0.0.1", "0.0.2", "0.0.3", "0.0.4"):
        d = env.prefix / "releases" / v
        d.mkdir()
    apply(env)
    left = sorted(p.name for p in (env.prefix / "releases").iterdir())
    assert "0.2.0" in left and "0.1.0" in left and len(left) <= updater.KEEP_RELEASES


# ---------- сторож ----------

def switch(env, now=1000.0, version="0.2.0", previous="0.1.0"):
    (env.prefix / "releases" / version / "homelab_agent").mkdir(parents=True, exist_ok=True)
    guard.replace_symlink(env.prefix / "current", f"releases/{version}")
    guard.write_switch(env.state, version, previous, 120, now)


def test_guard_rolls_back_without_mark(env):
    switch(env)
    restarts = []
    assert guard.check(env.prefix, env.state, 1060.0, lambda: restarts.append(1)) == "waiting"
    assert env.current() == "0.2.0" and not restarts
    assert guard.check(env.prefix, env.state, 1200.0, lambda: restarts.append(1)) == "rolled_back"
    assert env.current() == "0.1.0" and restarts == [1]
    assert not (env.state / "switch.json").exists()
    assert json.loads((env.state / "rollback.json").read_text())["from"] == "0.2.0"
    assert guard.check(env.prefix, env.state, 1300.0, lambda: restarts.append(1)) == "idle"


def test_guard_confirms_when_new_version_connected(env):
    switch(env)
    guard.write_mark("0.2.0", env.state, now=1050.0)
    assert guard.check(env.prefix, env.state, 5000.0, lambda: pytest.fail("restart")) == "confirmed"
    assert env.current() == "0.2.0" and not (env.state / "switch.json").exists()


def test_guard_ignores_mark_of_old_version(env):
    switch(env)
    guard.write_mark("0.1.0", env.state, now=1050.0)  # подключился ещё старый процесс до перезапуска
    assert guard.check(env.prefix, env.state, 1500.0, lambda: None) == "rolled_back"


def test_guard_distrusts_bad_switch_file(env):
    (env.state / "switch.json").write_text(json.dumps({"version": "../x", "previous": "0.1.0", "ts": 1,
                                                       "confirm_seconds": 1}))
    assert guard.check(env.prefix, env.state, 9999.0, lambda: None).startswith("error")
    assert env.current() == "0.1.0"


def test_write_mark_skips_missing_state_dir(tmp_path):
    guard.write_mark("1.0.0", tmp_path / "nope")
    assert not (tmp_path / "nope").exists()
    guard.write_mark("1.0.0", tmp_path, now=5)
    assert guard.read_mark(tmp_path) == ("1.0.0", 5.0)


# ---------- конфиг и объявление ----------

def test_parse_update():
    assert parse_update(None).enabled is False
    assert parse_update({}).restart_argv[:3] == ["sudo", "-n", "systemctl"]
    for bad in ({"enabled": "yes"}, {"confirm_seconds": 5}, {"restart_argv": "reboot"}, {"restart_argv": []}, {"x": 1}):
        with pytest.raises(ValueError):
            parse_update(bad)


def test_feature_announced_only_when_enabled():
    off = Agent(parse_config({"node": "pi"}), collectors=[])
    assert off.updater is None and "update" not in off.hello("t")["features"]
    on = Agent(parse_config({"node": "pi", "update": {"enabled": True}}), collectors=[])
    assert on.hello("t")["features"] == ["update"]
    both = Agent(parse_config({"node": "pi", "wol": {"enabled": True}, "update": {"enabled": True}}), collectors=[])
    assert both.hello("t")["features"] == ["wol", "update"]
