"""start-agent: флаги, блок «ОШИБКА на шаге…», отчёты --report-url (команды подменены в PATH, система не трогается)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
needs_env = pytest.mark.skipif(
    not shutil.which("bash") or not Path("/run/systemd/system").is_dir()
    or sys.platform != "linux",
    reason="нужен bash и systemd")


def _fake(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def _setup(tmp_path, install_body="exit 0"):
    """Копия start-agent с поддельным packaging/install.sh и командами id/curl/systemctl в PATH."""
    work = tmp_path / "agent"
    (work / "packaging").mkdir(parents=True)
    # В копии пути установки ведут во временный каталог: настоящая /opt/homelab-agent не затрагивается
    text = (ROOT / "start-agent").read_text(encoding="utf-8")
    text = text.replace("PREFIX=/opt/homelab-agent ", f"PREFIX={tmp_path}/prefix ")
    text = text.replace("OLD_ETC=/etc/homelab-agent ", f"OLD_ETC={tmp_path}/oldetc ")
    assert f"PREFIX={tmp_path}/prefix " in text and f"OLD_ETC={tmp_path}/oldetc " in text
    (work / "start-agent").write_text(text, encoding="utf-8")
    (work / "packaging" / "install.sh").write_text(f"#!/bin/sh\n{install_body}\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake(bindir / "id", "echo 0")  # «мы root», чтобы не запускать sudo
    _fake(bindir / "systemctl", "exit 0")
    reports = tmp_path / "reports.jsonl"
    _fake(bindir / "curl", f'cat >> "{reports}"; echo >> "{reports}"; exit 0')
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    return work / "start-agent", env, reports


def _run(script, env, *args):
    return subprocess.run(["bash", str(script), *args], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=60)


def _reports(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.mark.skipif(not shutil.which("bash"), reason="нужен bash")
def test_help_and_unknown_flag(tmp_path):
    script = ROOT / "start-agent"
    ok = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True)
    assert ok.returncode == 0 and "--token-file" in ok.stdout and "--report-url" in ok.stdout and "--vpn" in ok.stdout
    bad = subprocess.run(["bash", str(script), "--bogus"], capture_output=True, text=True)
    assert bad.returncode == 1 and "неизвестный аргумент" in bad.stderr
    missing = subprocess.run(["bash", str(script), "--node"], capture_output=True, text=True)
    assert missing.returncode == 1


@needs_env
def test_no_tty_without_token_fails_on_token_step(tmp_path):
    script, env, reports = _setup(tmp_path)
    res = _run(script, env, "--node", "example", "--panel-url", "wss://panel.example/agent/ws",
               "--report-url", "https://panel.example/agent/install/code/report",
               "--help-url", "https://panel.example/help")
    assert res.returncode == 1
    assert "ОШИБКА на шаге «token»" in (res.stdout + res.stderr)
    assert "Что сделать:" in (res.stdout + res.stderr)
    assert "Установка вручную: https://panel.example/help" in (res.stdout + res.stderr)
    msgs = _reports(reports)
    steps = [(m["step"], m["status"]) for m in msgs]
    assert steps[:4] == [("python", "running"), ("python", "ok"), ("code", "running"), ("code", "ok")]
    assert ("token", "running") in steps and ("token", "failed") in steps
    last = msgs[-1]
    assert last["step"] == "done" and last["status"] == "failed" and "log" in last
    assert all(len(m["message"]) <= 500 for m in msgs)


@needs_env
def test_install_failure_reports_code_step_and_default_manual_hint(tmp_path):
    script, env, reports = _setup(tmp_path, install_body="echo 'нет сети для pip' >&2\nexit 1")
    res = _run(script, env, "--report-url", "https://panel.example/r")
    assert res.returncode == 1
    assert "ОШИБКА на шаге «code»" in (res.stdout + res.stderr)
    assert "README.md, раздел «Установка вручную»" in (res.stdout + res.stderr)
    msgs = _reports(reports)
    assert msgs[-1]["step"] == "done" and "нет сети для pip" in msgs[-1]["log"]
    assert any(m["step"] == "code" and m["status"] == "failed" for m in msgs)


@needs_env
def test_report_url_is_validated_and_secret_not_reported(tmp_path):
    """Токен из файла не попадает в отчёты; неверный адрес отчётов молча отбрасывается."""
    script, env, reports = _setup(tmp_path, install_body="exit 1")
    token = tmp_path / "token"
    token.write_text("T" * 40 + "\n")
    res = _run(script, env, "--token-file", str(token), "--report-url", "file:///etc/passwd")
    assert res.returncode == 1 and not reports.exists()
    res = _run(script, env, "--token-file", str(token), "--report-url", "https://panel.example/r")
    assert "T" * 40 not in reports.read_text() + res.stdout + res.stderr
