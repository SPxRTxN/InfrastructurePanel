"""Статические проверки упаковки: синтаксис install.sh, юнит, sudoers, пример конфига."""
import configparser
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"


def unit():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(PKG / "homelab-agent.service", encoding="utf-8")
    return cp


@pytest.mark.skipif(not shutil.which("bash"), reason="нет bash")
def test_install_sh_syntax():
    res = subprocess.run(["bash", "-n", str(PKG / "install.sh")], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_install_sh_safety():
    text = (PKG / "install.sh").read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in text
    assert 'if [ ! -f "$ETC/agent.toml" ]' in text  # конфиг не перезаписывается
    assert "chmod 0600" in text and "-m 0640" in text  # токены 0600, agent.toml root:агент 0640
    assert "--upgrade" in text and "--display" in text
    # Установщик пишет в sudoers один файл (старый homelab-agent-update вливается и удаляется): перезапуск своей
    # службы и удаление по команде панели, проверено visudo; правила действий — владелец сам
    head = text.split("cat <<EOF")[0]
    assert head.count("/etc/sudoers.d") == 1 and "SUDOERS=\"$SUDOERS_DIR/homelab-agent\"" in head
    assert 'visudo -cf "$tmp_sudoers"' in head and "restart homelab-agent.service" in head
    assert "NOPASSWD: ALL" not in text
    assert "curl" not in text and "wget" not in text


def test_unit_file():
    s = unit()["Service"]
    assert s["User"] == "homelab-agent" and s["Group"] == "homelab-agent"
    # Раскладка с обновлениями: юнит смотрит на symlink current → releases/<версия>
    assert s["ExecStart"].startswith(
        "/opt/homelab-agent/app/current/.venv/bin/python -m homelab_agent -c /opt/homelab-agent/etc/agent.toml")
    assert s["WorkingDirectory"] == "/opt/homelab-agent/app/current"
    # Писать можно только в etc/, app/, state/ — не в bin/ и systemd/ (их исполняет root)
    assert s["ReadWritePaths"].split() == ["/opt/homelab-agent/etc", "/opt/homelab-agent/app", "/opt/homelab-agent/state"]
    assert s["Restart"] == "on-failure"
    for key, val in {"ProtectSystem": "strict", "ProtectHome": "true", "PrivateTmp": "true",
                     "DevicePolicy": "closed"}.items():
        assert s[key] == val
    assert "MemoryMax" in s
    # sudo несовместим с NoNewPrivileges=yes — в юните он только в комментарии
    assert "NoNewPrivileges" not in s
    assert unit()["Install"]["WantedBy"] == "multi-user.target"


def test_sudoers_example_is_narrow():
    rules = [line for line in (PKG / "sudoers.example").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert rules
    for rule in rules:
        assert re.fullmatch(r"homelab-agent ALL=\(root\) NOPASSWD: /usr/bin/systemctl restart [\w@.-]+\.service", rule)


@pytest.mark.parametrize("name", ["agent.example.toml", "agent.full.toml"])
def test_example_has_no_secrets(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    data = tomllib.loads(text)
    for panel in data["panel"]:
        assert "token" not in panel
        # ws:// допустим только с явным insecure = true
        assert panel["url"].startswith("wss://") or panel.get("insecure") is True
    assert data["display"]["enabled"] is False
    # install.sh включает дисплей заменой этой строки
    assert re.search(r"^enabled = false ", text, re.M)


def test_installed_example_has_only_universal_parts():
    """install.sh копирует agent.example.toml как есть: никаких сервисов, команд и действий из примеров."""
    data = tomllib.loads((ROOT / "agent.example.toml").read_text(encoding="utf-8"))
    assert len(data["panel"]) == 1
    assert set(data["collectors"]) == {"cpu", "memory", "disks", "thermal", "uptime"}
    assert "actions" not in data


def test_example_actions_match_sudoers():
    data = tomllib.loads((ROOT / "agent.full.toml").read_text(encoding="utf-8"))
    sudoers = (PKG / "sudoers.example").read_text(encoding="utf-8")
    for spec in data["actions"].values():
        argv = spec["argv"]
        if argv[0] == "sudo":
            cmd = " ".join(a for a in argv[1:] if a != "-n")
            assert f"NOPASSWD: /usr/bin/{cmd}" in sudoers


def test_gitignore_protects_config_and_tokens():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "agent.toml" in text.splitlines() and "token-*" in text


def test_requirements_pinned():
    for name in ("requirements.txt", "requirements-display.txt", "requirements-dev.txt"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "-r")):
                assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[\w.]+", line), line
