"""Скрипты для владельца: start-agent / uninstall-agent (Linux), .ps1 (Windows), push-agent.sh (дашборд)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PUSH = ROOT / "push-agent"
needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="нужен bash")


@needs_bash
@pytest.mark.parametrize("name", ["start-agent", "uninstall-agent", "push-agent"])
def test_linux_scripts_parse(name):
    assert subprocess.run(["bash", "-n", str(ROOT / name)]).returncode == 0


def test_start_agent_has_empty_token_line():
    text = (ROOT / "start-agent").read_text(encoding="utf-8")
    assert re.search(r'^TOKEN=""$', text, re.M), "в репозитории токен всегда пустой"
    assert "systemctl enable" in text and "sed -i 's/^TOKEN=.*/TOKEN=\"\"/'" in text


@pytest.mark.parametrize("name", ["start-agent.ps1", "uninstall-agent.ps1"])
def test_ps1_has_bom_for_windows_powershell(name):
    # Windows PowerShell 5.1 читает файл без BOM как ANSI — кириллица превратится в мусор
    assert (ROOT / name).read_bytes().startswith(b"\xef\xbb\xbf")


def test_start_agent_ps1_token_and_task():
    text = (ROOT / "start-agent.ps1").read_text(encoding="utf-8-sig")
    assert re.search(r'^\$TOKEN = ""$', text, re.M)
    assert "Register-ScheduledTask" in text and "-AtStartup" in text and "S-1-5-19" in text
    assert "UTF8Encoding $false" in text  # agent.toml без BOM — tomllib его не примет


@needs_bash
def test_push_agent_copies_only_install_files(tmp_path):
    """Подменяем scp: проверяем, что уходит на узел."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    dest = tmp_path / "remote"
    dest.mkdir()
    (fakebin / "scp").write_text(f'#!/bin/sh\n# последние два аргумента: источник и цель\nfor a; do :; done\n'
                                 f'eval src=\\${{$(($#-1))}}\ncp -rp "$src" "{dest}/"\n')
    (fakebin / "scp").chmod(0o755)
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"}
    env.pop("SUDO_USER", None)
    r = subprocess.run(["bash", str(PUSH), "pi@192.168.1.10"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = dest / "homelab-agent"
    assert (got / "start-agent").stat().st_mode & 0o111
    assert (got / "homelab_agent" / "__main__.py").exists() and (got / "УДАЛЕНИЕ.md").exists()
    assert not (got / "tests").exists() and not (got / "Obsidian").exists() and not (got / "push-agent").exists()
    assert not list(got.rglob("__pycache__"))
    assert "./start-agent" in r.stdout
