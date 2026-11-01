"""pre-commit не пускает в коммит секреты и боевые данные."""
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "tools" / "git-hooks" / "pre-commit"
pytestmark = pytest.mark.skipif(not shutil.which("git") or not shutil.which("sh"), reason="нужны git и sh")


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _hook(repo: Path, *files: tuple[str, str]) -> subprocess.CompletedProcess:
    for name, text in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        subprocess.run(["git", "-C", str(repo), "add", "-f", name], check=True)
    return subprocess.run(["sh", str(HOOK)], cwd=repo, capture_output=True, text=True)


@pytest.mark.parametrize("name", [".env", "secrets/secret_key", "data/homelab.db", "backups/x.db.age",
                                  "ssh/id_ed25519", "inbox/friend/CLAUDE.md"])
def test_blocks_secret_files(tmp_path, name):
    r = _hook(_repo(tmp_path), (name, "x"))
    assert r.returncode == 1 and name in r.stderr


def test_blocks_private_key_lines_without_printing_them(tmp_path):
    r = _hook(_repo(tmp_path), ("notes.md", "AGE-SECRET-KEY-1QQQQSECRET\n"))
    assert r.returncode == 1 and "1QQQQ" not in r.stderr


def test_allows_normal_files(tmp_path):
    r = _hook(_repo(tmp_path), (".env.example", "SECRET_KEY=\n"), ("inbox/README.md", "x"), ("app/x.py", "print(1)\n"))
    assert r.returncode == 0, r.stderr
