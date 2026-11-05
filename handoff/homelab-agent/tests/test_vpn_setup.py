"""packaging/vpn-setup: выбор архитектуры, проверка суммы, ошибки (curl подменён, root и сеть не нужны)."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "packaging" / "vpn-setup"
pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="нужен bash")

SUMS = {
    "amd64": "699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968",
    "arm64": "436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26",
    "armv7": "fd164ed45ebc784f657a3e1d0e0ad0f68afdcff864ee57122c2cfed3ddb80a00",
}


def _call(snippet, env=None):
    """Загружаем функции скрипта (при source main не запускается) и выполняем snippet."""
    return subprocess.run(["bash", "-c", f'source "{SETUP}"; {snippet}'], capture_output=True, text=True, env=env)


def test_syntax_and_usage():
    assert subprocess.run(["bash", "-n", str(SETUP)]).returncode == 0
    res = subprocess.run(["bash", str(SETUP), "bogus"], capture_output=True, text=True)
    assert res.returncode == 2 and "install|install-xray|status" in res.stderr


def test_pinned_version_and_sums_in_script():
    text = SETUP.read_text(encoding="utf-8")
    assert 'VERSION="1.14.4"' in text
    for s in SUMS.values():
        assert s in text


@pytest.mark.parametrize("machine,arch", [("x86_64", "amd64"), ("aarch64", "arm64"), ("armv7l", "armv7"),
                                           ("riscv64", ""), ("i686", "")])
def test_arch_mapping(machine, arch):
    assert _call(f'map_arch {machine}').stdout.strip() == arch


@pytest.mark.parametrize("arch", sorted(SUMS))
def test_expected_sum(arch):
    assert _call(f'expected_sum {arch}').stdout.strip() == SUMS[arch]


def _fake_curl(tmp_path, content: bytes | None):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    src = tmp_path / "payload"
    if content is not None:
        src.write_bytes(content)
    curl = bindir / "curl"
    # настоящий curl не вызывается: файл назначения (после -o) заполняется заготовкой или команда «падает»
    curl.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
                    + (f'cp "{src}" "$out"\n' if content is not None else 'exit 6\n'))
    curl.chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}


def test_fetch_wrong_sum_is_refused(tmp_path):
    env = _fake_curl(tmp_path, b"not the archive")
    work = tmp_path / "w"
    work.mkdir()
    res = _call(f'fetch_archive amd64 "{work}"', env)
    assert res.returncode == 1
    assert "контрольная сумма" in res.stderr and "Что сделать" in res.stderr and "github.com/SagerNet" in res.stderr


def test_fetch_download_failure_message(tmp_path):
    env = _fake_curl(tmp_path, None)
    work = tmp_path / "w"
    work.mkdir()
    res = _call(f'fetch_archive arm64 "{work}"', env)
    assert res.returncode == 1 and "не удалось скачать" in res.stderr and "sing-box-1.14.4-linux-arm64" in res.stderr


def test_fetch_matching_sum_passes(tmp_path):
    """Подменяем ожидаемую сумму на сумму заготовки — проверяем ветку успеха."""
    payload = b"fake archive"
    env = _fake_curl(tmp_path, payload)
    work = tmp_path / "w"
    work.mkdir()
    digest = hashlib.sha256(payload).hexdigest()
    res = _call(f'SUM_AMD64={digest}; fetch_archive amd64 "{work}"', env)
    assert res.returncode == 0
    assert res.stdout.strip().endswith("sing-box-1.14.4-linux-amd64.tar.gz")


def test_install_requires_root():
    if os.geteuid() == 0:
        pytest.skip("тест для обычного пользователя")
    res = subprocess.run(["bash", str(SETUP), "install"], capture_output=True, text=True)
    assert res.returncode == 1 and "нужен root" in res.stderr
