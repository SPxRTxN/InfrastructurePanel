"""Фиксирует все зависимости приложения (включая транзитивные) в requirements.lock.

Берёт пакеты из requirements.txt, рекурсивно обходит их зависимости по метаданным
установленных пакетов в .venv и пишет `имя==версия`. Docker и CI ставят пакеты
с `-c requirements.lock`, поэтому сборка на любом устройстве получает те же версии.

    .venv/bin/python tools/lock_deps.py          # переписать requirements.lock
    .venv/bin/python tools/lock_deps.py --check  # код 1, если lock устарел (CI)

Маркеры окружения (`sys_platform`, `python_version`) вычисляются для текущей машины;
для linux/amd64 и linux/arm64 с Python 3.12 набор одинаковый.
"""
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock"


def top_level() -> list[Requirement]:
    lines = (ROOT / "requirements.txt").read_text().splitlines()
    return [Requirement(x.split("#")[0].strip()) for x in lines if x.split("#")[0].strip()]


def resolve() -> dict[str, str]:
    pinned: dict[str, str] = {}
    # Пакеты для другой архитектуры (sqlcipher3 на aarch64) здесь не установлены — их фиксирует requirements.txt
    queue = [(r, set(r.extras)) for r in top_level() if r.marker is None or r.marker.evaluate()]
    while queue:
        req, extras = queue.pop()
        name = canonicalize_name(req.name)
        if name in pinned:
            continue
        dist = metadata.distribution(req.name)
        pinned[name] = dist.version
        for raw in dist.requires or []:
            sub = Requirement(raw)
            envs = [{"extra": e} for e in extras] or [{"extra": ""}]
            if sub.marker is None or any(sub.marker.evaluate(env) for env in envs):
                queue.append((sub, set(sub.extras)))
    return pinned


def render(pinned: dict[str, str]) -> str:
    head = "# Сгенерировано tools/lock_deps.py — не редактировать вручную\n"
    return head + "".join(f"{n}=={v}\n" for n, v in sorted(pinned.items()))


def main() -> int:
    text = render(resolve())
    if "--check" in sys.argv:
        if not LOCK.exists() or LOCK.read_text() != text:
            print("requirements.lock устарел: запустите python tools/lock_deps.py")
            return 1
        print("requirements.lock актуален")
        return 0
    LOCK.write_text(text)
    print(f"requirements.lock: {text.count(chr(10)) - 1} пакетов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
