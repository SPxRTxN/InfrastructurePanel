"""Есть ли готовые сборки (wheels) всех зависимостей под платформы, куда мы выкладываемся.

Docker ставит пакеты с `--only-binary=:all:` — без компилятора. Если для какой-то версии из
requirements.lock нет wheel под arm64 (Raspberry Pi) или Windows, образ упадёт посреди сборки;
этот скрипт ловит это заранее (pip install --dry-run, ничего не ставит, только спрашивает PyPI).

    .venv/bin/python tools/check_wheels.py                 # linux amd64 + arm64, Python 3.12
    .venv/bin/python tools/check_wheels.py --windows       # + win_amd64 (нативный запуск без Docker)
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLATFORMS = {
    "linux/amd64": ["manylinux_2_34_x86_64", "manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64"],
    "linux/arm64": ["manylinux_2_34_aarch64", "manylinux_2_28_aarch64", "manylinux_2_17_aarch64", "manylinux2014_aarch64"],
}
WINDOWS = {"windows/amd64": ["win_amd64"]}


def check(name: str, tags: list[str], python: str = "3.12") -> bool:
    with tempfile.TemporaryDirectory() as target:
        cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--ignore-installed",
               "--only-binary=:all:", "--python-version", python, "--implementation", "cp", "--target", target,
               "-r", str(ROOT / "requirements.txt"), "-c", str(ROOT / "requirements.lock")]
        for tag in tags:
            cmd += ["--platform", tag]
        r = subprocess.run(cmd, capture_output=True, text=True)
    status = "ok" if r.returncode == 0 else "НЕТ wheel"
    print(f"{name:15} {status}")
    if r.returncode:
        print("   " + (r.stderr.strip().splitlines() or ["?"])[-1])
    return r.returncode == 0


def main(argv: list[str]) -> int:
    targets = dict(PLATFORMS)
    if "--windows" in argv:
        targets |= WINDOWS
    results = [check(name, tags) for name, tags in targets.items()]
    # Нативная установка берёт Python ОС: Debian 12 / Pi OS bookworm — 3.11, Debian 13 / trixie — 3.13
    results += [check(f"arm64 py{v}", PLATFORMS["linux/arm64"], v) for v in ("3.11", "3.13")]
    results += [check(f"amd64 py{v}", PLATFORMS["linux/amd64"], v) for v in ("3.11", "3.13")]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
