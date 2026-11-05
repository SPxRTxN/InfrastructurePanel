"""packaging/python-setup и --own-python: свой Python агента отдельно от системы (без сети и root)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "packaging" / "python-setup"
pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="нужен bash")


def _call(snippet):
    return subprocess.run(["bash", "-c", f'source "{SETUP}"; {snippet}'], capture_output=True, text=True)


def test_syntax_usage_and_triples():
    assert subprocess.run(["bash", "-n", str(SETUP)]).returncode == 0
    assert subprocess.run(["bash", str(SETUP), "bogus"], capture_output=True).returncode == 2
    out = _call("pbs_triple x86_64; pbs_triple aarch64; pbs_triple armv7l; pbs_triple mips").stdout.split("\n")
    assert out[:4] == ["x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "armv7-unknown-linux-gnueabihf", ""]


def test_path_only_when_installed(tmp_path):
    assert _call(f'do_path "{tmp_path}"').stdout == ""
    py = tmp_path / "python" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    assert _call(f'do_path "{tmp_path}"').stdout.strip() == str(py)


def test_install_refuses_without_pins(tmp_path):
    # Суммы не закреплены — отказ до скачивания (id -u подменён: проверка root не мешает тесту)
    res = _call(f'id() {{ echo 0; }}; do_install "{tmp_path}"')
    if "не закреплены" not in res.stderr:  # владелец вписал суммы — тогда отказ дальше, но не молча
        assert res.returncode != 0 or (tmp_path / "python").exists()
    else:
        assert res.returncode == 1 and not (tmp_path / "python").exists()


def test_installers_pass_own_python():
    start = (ROOT / "start-agent").read_text(encoding="utf-8")
    inst = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
    assert "--own-python) OWN_PY=1" in start and 'ARGS+=(--own-python)' in start
    assert "--own-python) OWN_PY=1" in inst and '"$PY" -m venv "$REL/.venv"' in inst
    assert "python3 -m venv" not in inst  # venv — только от выбранного Python


def test_pins_are_set():
    text = SETUP.read_text(encoding="utf-8")
    import re
    assert re.search(r'^PBS_TAG="\d{8}"$', text, re.M) and re.search(r'^PY_VERSION="3\.12\.\d+"$', text, re.M)
    for arch in ("X86_64", "AARCH64", "ARMV7"):
        assert re.search(rf'^PSUM_{arch}="[0-9a-f]{{64}}"$', text, re.M), arch


def test_install_moves_tmp_when_full():
    inst = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
    i, j = inst.index("export TMPDIR=/var/tmp"), inst.index('"$PY" -m venv "$REL/.venv"')
    assert i < j  # до создания venv



def test_temp_dir_removed_after_successful_install(tmp_path):
    """Регресс: trap EXIT с local tmp срабатывал после выхода из функции — при успешной установке sing-box
    архив и бинарник (~100 МБ) оставались в /tmp; на Pi (tmpfs) /tmp забивался и падал ensurepip."""
    work = tmp_path / "tmp"
    work.mkdir()
    snippet = (f'export TMPDIR="{work}"; source "{ROOT}/packaging/vpn-setup"; SYSD_DIR="{tmp_path}/none"; '
               'id() { echo 0; }; install() { :; }; tar() { :; }; '
               'fetch_archive() { : > "$2/a.tar.gz"; echo "$2/a.tar.gz"; }; '
               '(do_install) && echo OK')
    res = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    assert "OK" in res.stdout, res.stderr
    assert list(work.iterdir()) == []


def test_install_recreates_venv_without_pip():
    inst = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
    assert '! "$REL/.venv/bin/python" -m pip --version' in inst
    assert inst.index("-m pip --version") < inst.index('"$PY" -m venv "$REL/.venv"')
