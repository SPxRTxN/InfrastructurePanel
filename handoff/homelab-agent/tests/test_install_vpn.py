"""install.sh --vpn: включение [vpn] в agent.toml (идемпотентно), строки sudoers, вызов vpn-setup."""
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")


def _enable_script() -> str:
    """Тело python-вставки из функции enable_vpn_in_config — запускаем её на временном файле."""
    m = re.search(r"enable_vpn_in_config\(\) \{\n    python3 - \"\$ETC/agent.toml\" <<'PY'\n(.*?)\nPY\n", INSTALL, re.S)
    assert m, "не найдена enable_vpn_in_config"
    return m.group(1)


def _enable(path: Path) -> None:
    subprocess.run(["python3", "-c", _enable_script(), str(path)], check=True)


@pytest.mark.parametrize("before", [
    'node = "n"\n[panel]\nurl = "wss://p.example"\n',
    'node = "n"\n[vpn]\nenabled = false\nrollback_seconds = 30\n[wol]\nx = 1\n',
    'node = "n"\n[vpn]\nrollback_seconds = 30\n',
])
def test_enable_vpn_is_idempotent(tmp_path, before):
    conf = tmp_path / "agent.toml"
    conf.write_text(before, encoding="utf-8")
    _enable(conf)
    once = conf.read_text(encoding="utf-8")
    assert tomllib.loads(once)["vpn"]["enabled"] is True
    _enable(conf)
    assert conf.read_text(encoding="utf-8") == once
    assert once.count("[vpn]") == 1


def test_install_vpn_wiring():
    assert 'install -m 0755 -o root -g root "$SRC/packaging/vpn-setup" "$BIN/vpn-setup"' in INSTALL
    assert 'NOPASSWD: $SETUP_RUN $BIN/vpn-setup install' in INSTALL
    assert '"$BIN/vpn-setup" install ||' in INSTALL  # сбой установки sing-box — предупреждение, не ошибка
    assert "--vpn) VPN_OPT=1" in INSTALL


def test_vpn_setup_is_executable_and_shipped():
    assert (ROOT / "packaging" / "vpn-setup").stat().st_mode & 0o111
    assert shutil.which("bash")
