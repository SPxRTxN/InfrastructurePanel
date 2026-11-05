"""Настройки и удаление из панели: managed/local_only, panel.toml, uninstall, features, пути по умолчанию."""
import asyncio
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from homelab_agent import config as config_mod
from homelab_agent import guard, remote_config
from homelab_agent.config import ConfigError, load_config, parse_config
from homelab_agent.remote_config import RemoteError, apply_config_set, dump_toml
from homelab_agent.runner import Agent

ROOT = Path(__file__).resolve().parent.parent
BASE = """
node = "pi"
[[panel]]
url = "ws://127.0.0.1:1/agent/ws"
insecure = true
token = "t"
[collectors.cpu]
[collectors.command]
items = [ { name = "x", argv = ["/bin/true"], parse = "first_int" } ]
[actions.restart_nginx]
argv = ["sudo", "-n", "systemctl", "restart", "nginx.service"]
[update]
enabled = true
restart_argv = ["sudo", "-n", "systemctl", "restart", "homelab-agent.service"]
"""


@pytest.fixture
def etc(tmp_path, monkeypatch):
    d = tmp_path / "etc"
    d.mkdir()
    (d / "agent.toml").write_text(BASE, encoding="utf-8")
    monkeypatch.setattr(guard, "ETC_DIR", str(d))
    return d


# ---------- запись TOML ----------

def test_dump_toml_roundtrip():
    data = {"interval": 5, "collectors": {"cpu": {}, "checks": {"items": [{"name": "a b", "port": 80, "ok": True}]}},
            "display": {"enabled": False, "screens": ["a", "б"], "night_start": "23:00", "k": 1.5}}
    assert tomllib.loads(dump_toml(data)) == data


def test_dump_toml_rejects_unwritable():
    with pytest.raises(RemoteError):
        dump_toml({"display": {"x": None}})
    with pytest.raises(RemoteError):
        dump_toml({"interval": float("inf")})


# ---------- загрузка: agent.toml + panel.toml ----------

def test_load_without_panel_toml_is_as_before(etc):
    cfg = load_config(str(etc / "agent.toml"))
    assert cfg.interval == 10 and "command" in cfg.collectors and cfg.remote == {"config": True, "uninstall": True}


def test_panel_toml_overrides_managed_only(etc):
    (etc / "panel.toml").write_text(
        'interval = 5\n[collectors.memory]\n[collectors.command]\nitems = []\n'
        '[update]\nenabled = false\nrestart_argv = ["sh", "-c", "evil"]\n'
        '[actions.evil]\nargv = ["rm"]\n', encoding="utf-8")
    cfg = load_config(str(etc / "agent.toml"))
    assert cfg.interval == 5
    assert set(cfg.collectors) == {"memory", "command"}
    assert cfg.collectors["command"]["items"][0]["name"] == "x"  # command — только из agent.toml
    assert cfg.update["enabled"] is False and "evil" not in str(cfg.update)  # restart_argv из панели отброшен, свой — остался
    assert set(cfg.actions) == {"restart_nginx"}  # actions из panel.toml игнорируются


def test_broken_panel_toml_does_not_break_agent(etc):
    (etc / "panel.toml").write_text("interval = [", encoding="utf-8")
    assert load_config(str(etc / "agent.toml")).interval == 10
    (etc / "panel.toml").write_text("interval = 99999\n", encoding="utf-8")  # синтаксис верен, значение — нет
    assert load_config(str(etc / "agent.toml")).interval == 10


def test_remote_section_validation():
    with pytest.raises(ConfigError):
        parse_config({"node": "x", "remote": {"config": "yes"}})
    with pytest.raises(ConfigError):
        parse_config({"node": "x", "remote": {"other": True}})
    assert parse_config({"node": "x", "remote": {"uninstall": False}}).remote == {"config": True, "uninstall": False}


# ---------- config_set ----------

def test_config_set_writes_panel_toml(etc):
    msg = apply_config_set(str(etc / "agent.toml"), {"interval": 7, "wol": {"enabled": True}})
    assert "сохранены" in msg
    p = etc / "panel.toml"
    assert oct(p.stat().st_mode & 0o777) == "0o600" and not (etc / "panel.toml.tmp").exists()
    assert tomllib.loads(p.read_text(encoding="utf-8")) == {"interval": 7, "wol": {"enabled": True}}
    # следующий set меняет присланные разделы, остальные сохраняются
    apply_config_set(str(etc / "agent.toml"), {"interval": 9})
    assert tomllib.loads(p.read_text(encoding="utf-8")) == {"interval": 9, "wol": {"enabled": True}}
    cfg = load_config(str(etc / "agent.toml"))
    assert cfg.interval == 9 and cfg.wol == {"enabled": True}


@pytest.mark.parametrize("sections", [
    {"panel": {"url": "wss://x"}},
    {"actions": {"a": {"argv": ["rm"]}}},
    {"vpn": {"enabled": True}},
    {"node": "other"},
    {"remote": {"config": True}},
    {"collectors": {"cpu": {}, "command": {"items": []}}},
    {"update": {"enabled": True, "restart_argv": ["sh", "-c", "x"]}},
    {"interval": 99999},
    {"collectors": {"nonexistent": {}}},
    {"display": {"x": None}},
    {},
    [],
    {"wol": "строка"},
])
def test_config_set_refuses(etc, sections):
    with pytest.raises(RemoteError):
        apply_config_set(str(etc / "agent.toml"), sections)
    assert not (etc / "panel.toml").exists()


def test_config_set_write_is_atomic(etc, monkeypatch):
    apply_config_set(str(etc / "agent.toml"), {"interval": 7})
    before = (etc / "panel.toml").read_text(encoding="utf-8")

    def boom(*a, **k):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RemoteError):
        apply_config_set(str(etc / "agent.toml"), {"interval": 8})
    assert (etc / "panel.toml").read_text(encoding="utf-8") == before  # прежний файл цел
    assert not (etc / "panel.toml.tmp").exists()


# ---------- обработка сообщений агентом ----------

class FakeClient:
    def __init__(self):
        self.sent = []

    def send_control(self, msg):
        self.sent.append(msg)

    async def flush(self, timeout=5.0):
        pass


def make_agent(etc, extra=""):
    (etc / "agent.toml").write_text(BASE + extra, encoding="utf-8")
    agent = Agent(load_config(str(etc / "agent.toml")), collectors=[])
    agent.remote_delay = 0
    agent.run_cmd = lambda argv, cwd, timeout: calls.append(("restart", argv)) or (0, "")
    agent.launch_uninstall = lambda: calls.append(("uninstall",)) or 0
    calls.clear()
    return agent


calls: list = []


def handle(agent, msg):
    client = FakeClient()
    asyncio.run(agent.handle_remote(client, msg))
    return client.sent


def test_features_and_layout(etc, tmp_path):
    agent = make_agent(etc)
    assert agent.hello("t")["features"] == ["update", "config", "uninstall"]
    # конфиг вне <папка>/etc (старая раскладка, разработка) — возможности не объявляются
    other = Agent(parse_config({"node": "pi"}, str(tmp_path / "agent.toml")), collectors=[])
    assert other.hello("t")["features"] == []
    off = make_agent(etc, "\n[remote]\nconfig = false\n")
    assert off.hello("t")["features"] == ["update", "uninstall"]


def test_config_get(etc):
    out = handle(make_agent(etc), {"type": "config_get", "id": "c-1"})[0]
    assert out["type"] == "config" and out["id"] == "c-1"
    assert out["managed"] == ["interval", "collectors", "display", "wol", "update", "terminal"]
    assert out["local_only"] == ["panel", "actions", "vpn", "collectors.command", "terminal.target"]
    assert set(out["sections"]) == set(out["managed"])
    assert "command" not in out["sections"]["collectors"] and "restart_argv" not in out["sections"]["update"]
    assert "token" not in str(out)


def test_config_set_flow_restarts(etc):
    agent = make_agent(etc)
    out = handle(agent, {"type": "config_set", "id": "c-2", "sections": {"interval": 20}})
    assert out == [{"type": "config_result", "id": "c-2", "ok": True, "detail": out[0]["detail"]}]
    assert calls == [("restart", ["sudo", "-n", "systemctl", "restart", "homelab-agent.service"])]
    assert load_config(str(etc / "agent.toml")).interval == 20


def test_config_set_refusal_does_not_restart(etc):
    out = handle(make_agent(etc), {"type": "config_set", "id": "c-3", "sections": {"actions": {}}})
    assert out[0]["ok"] is False and "только на узле" in out[0]["detail"] and calls == []


def test_uninstall_launches_fixed_command(etc):
    out = handle(make_agent(etc), {"type": "uninstall", "id": "x-1", "argv": ["rm", "-rf", "/"]})
    assert out == [{"type": "uninstall_result", "id": "x-1", "ok": True, "detail": "удаление запускается"}]
    assert calls == [("uninstall",)]


def test_launch_uninstall_argv_is_constant():
    seen = []
    remote_config.launch_uninstall(run=lambda argv: seen.append(argv) or 0)
    assert seen == [["sudo", "-n", "/opt/homelab-agent/bin/uninstall-agent", "--from-panel"]]


def test_disabled_on_node(etc):
    agent = make_agent(etc, "\n[remote]\nuninstall = false\nconfig = false\n")
    for msg in ({"type": "uninstall", "id": "x"}, {"type": "config_get", "id": "c"},
                {"type": "config_set", "id": "c", "sections": {"interval": 5}}):
        out = handle(agent, msg)
        assert out[0]["ok"] is False
    assert calls == [] and not (etc / "panel.toml").exists()


# ---------- пути ----------

def test_default_paths():
    assert guard.ETC_DIR == "/opt/homelab-agent/etc" and guard.APP_DIR == "/opt/homelab-agent/app"
    assert guard.STATE_DIR == "/opt/homelab-agent/state"
    if os.name != "nt":
        assert config_mod.DEFAULT_PATH == "/opt/homelab-agent/etc/agent.toml"
        assert config_mod.LEGACY_PATH == "/etc/homelab-agent/agent.toml"


def test_default_path_falls_back_to_legacy(tmp_path, monkeypatch):
    new, old = tmp_path / "new.toml", tmp_path / "old.toml"
    monkeypatch.setattr(config_mod, "DEFAULT_PATH", str(new))
    monkeypatch.setattr(config_mod, "LEGACY_PATH", str(old))
    assert config_mod.default_path() == str(new)  # ничего нет — основной путь (ошибка «нет файла» понятна)
    old.write_text("", encoding="utf-8")
    assert config_mod.default_path() == str(old)
    new.write_text("", encoding="utf-8")
    assert config_mod.default_path() == str(new)


def test_state_dir_default(monkeypatch):
    monkeypatch.delenv("HOMELAB_AGENT_STATE", raising=False)
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    assert str(guard.state_dir()) == "/opt/homelab-agent/state"


# ---------- статические проверки упаковки ----------

INSTALL = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="нужен bash")


@needs_bash
@pytest.mark.parametrize("name", ["packaging/install.sh", "start-agent", "uninstall-agent", "push-agent"])
def test_scripts_parse(name):
    assert subprocess.run(["bash", "-n", str(ROOT / name)]).returncode == 0


@needs_bash
def test_tools_push_agent_parses():
    wrapper = ROOT.parent.parent / "tools" / "push-agent.sh"
    if wrapper.exists():
        assert subprocess.run(["bash", "-n", str(wrapper)]).returncode == 0


def test_install_migrates_old_layout():
    for old in ("OLD_ETC=/etc/homelab-agent", "OLD_LIB=/usr/local/lib/homelab-agent", "OLD_STATE=/var/lib/homelab-agent"):
        assert old in INSTALL
    assert "for n in releases current previous" in INSTALL
    # удаление старого — только после проверки переноса и не раньше, чем всё скопировано
    assert INSTALL.index("all_copied") < INSTALL.index('rm -r -f -- "$OLD_ETC"')
    assert "MOVE_OLD_ETC=1" in INSTALL and 'if [ "$MOVE_OLD_ETC" -eq 1 ]' in INSTALL
    assert 'sed -i "s|$OLD_ETC|$ETC|g"' in INSTALL  # пути токенов в agent.toml


def test_install_permissions():
    # родитель — root:root 0755, иначе агент подменит bin/; bin/ и systemd/ — root
    assert 'install -d -m 0755 -o root -g root "$ROOT" "$BIN" "$SYSD"' in INSTALL
    assert 'chown root:root "$ROOT"' in INSTALL
    assert 'chown "$APP_USER:$APP_USER" "$ROOT"' not in INSTALL
    assert re.search(r'chown -R "\$APP_USER:\$APP_USER" "\$(APP|STATE|ETC/vpn)', INSTALL)
    assert not re.search(r'chown[^\n]*"\$(BIN|SYSD)', INSTALL)
    assert 'chmod 1770 "$ETC"' in INSTALL


def test_install_units_linked_from_systemd_dir():
    assert 'mv -T -- "$UNIT_DIR/$name.new" "$UNIT_DIR/$name"' in INSTALL
    assert '"$SYSD/$name"' in INSTALL and "install -m 0644 -o root -g root \"$SRC/packaging/homelab-agent.service\"" in INSTALL


def test_install_single_sudoers_file_with_visudo():
    assert INSTALL.count("/etc/sudoers.d") == 1
    assert 'SUDOERS="$SUDOERS_DIR/homelab-agent"' in INSTALL and "homelab-agent-update" in INSTALL
    assert 'rm -f -- "$OLD_SUDOERS"' in INSTALL
    assert INSTALL.index('visudo -cf "$tmp_sudoers"') < INSTALL.index('install -m 0440 -o root -g root "$tmp_sudoers" "$SUDOERS"')
    rules = [ln for ln in INSTALL.splitlines() if "NOPASSWD:" in ln and ln.lstrip().startswith("echo")]
    active = [r for r in rules if "echo \"#" not in r]
    # две безусловные строки + шесть VPN-строк, которые идут только в ветке --vpn (с отступом глубже)
    base = [r for r in active if r.startswith("        echo")]
    assert len(base) == 2
    assert len(active) - len(base) == 6 and all("vpn" in r for r in active if r not in base)
    assert any("restart homelab-agent.service" in r for r in active)
    assert any('$BIN/uninstall-agent --from-panel' in r for r in active)
    assert "NOPASSWD: ALL" not in INSTALL


def test_install_appends_update_section_on_upgrade():
    assert '[ "$UPGRADE" -eq 1 ] && ! grep -Eq \'^[[:space:]]*\\[update\\]\' "$ETC/agent.toml"' in INSTALL
    assert "enabled = true" in INSTALL.split("дописан раздел")[0].split("[update]")[-1]


def test_install_does_not_copy_token_bearing_start_agent():
    # start-agent с вписанным токеном нельзя класть в world-readable bin/
    assert 'install -m 0755 -o root -g root "$SRC/uninstall-agent"' in INSTALL
    assert "start-agent" not in INSTALL.split("# bin/")[1].split("Виртуальное окружение")[0]


def test_uninstall_agent_from_panel():
    text = (ROOT / "uninstall-agent").read_text(encoding="utf-8")
    assert '"--from-panel"' in text and "systemd-run --no-block" in text and "--collector" in text
    assert "/run/homelab-uninstall" in text  # копия себя: папка со скриптом удаляется
    for gone in ("/opt/homelab-agent", "/etc/homelab-agent", "/var/lib/homelab-agent", "/usr/local/lib/homelab-agent",
                 "/etc/sudoers.d/homelab-agent", "userdel homelab-agent"):
        assert gone in text


def test_push_agent_install_flag():
    text = (ROOT / "push-agent").read_text(encoding="utf-8")
    assert "--install" in text and 'ssh -t "$target" "sudo bash ~/homelab-agent/start-agent"' in text


def test_sudoers_example_documents_new_rules():
    text = (ROOT / "packaging" / "sudoers.example").read_text(encoding="utf-8")
    assert "uninstall-agent --from-panel" in text


def test_vpn_unit_reads_config_via_credential():
    """Root службы без CAP_DAC_OVERRIDE не читает файл агента (0700) — конфиг передаёт systemd (LoadCredential)."""
    text = (ROOT / "packaging" / "homelab-vpn.service").read_text(encoding="utf-8")
    assert "LoadCredential=config.json:/opt/homelab-agent/etc/vpn/config.json" in text
    assert "run -c %d/config.json" in text and "CAP_DAC_OVERRIDE" not in text.split("CapabilityBoundingSet=")[1]


def test_units_do_not_use_old_paths():
    for name in ("homelab-agent.service", "homelab-agent-guard.service", "homelab-vpn.service"):
        text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
        assert "/etc/homelab-agent" not in text and "/var/lib/homelab-agent" not in text
        assert "/usr/local/lib/homelab-agent" not in text
    guard_unit = (ROOT / "packaging" / "homelab-agent-guard.service").read_text(encoding="utf-8")
    assert "/opt/homelab-agent/bin/guard.py /opt/homelab-agent/app" in guard_unit


def test_terminal_from_panel_keeps_local_target():
    """Панель включает консоль через агента и задаёт лимиты, но адрес пересылки — только из agent.toml."""
    from homelab_agent.remote_config import merge
    base = {"terminal": {"enabled": False, "target": "127.0.0.1:2222"}}
    out = merge(base, {"terminal": {"enabled": True, "max_sessions": 3, "target": "10.0.0.5:22"}})
    assert out["terminal"] == {"enabled": True, "max_sessions": 3, "target": "127.0.0.1:2222"}
    assert merge({}, {"terminal": {"enabled": True, "target": "10.0.0.5:22"}})["terminal"] == {"enabled": True}
