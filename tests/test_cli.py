from sqlalchemy import select

from app import cli
from app.models import User


def _run(monkeypatch, username, passwords):
    answers = iter(passwords)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: username)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(answers))
    return cli.main(["create-admin"])


def test_create_admin(monkeypatch, db, capsys):
    assert _run(monkeypatch, "root", ["very-long-pass", "very-long-pass"]) == 0
    user = db.scalar(select(User).where(User.username == "root"))
    assert user.can("users.manage") and user.password_hash.startswith("$argon2id$")
    assert "very-long-pass" not in capsys.readouterr().out


def test_create_admin_needs_terminal(monkeypatch, db, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["create-admin"]) == 1
    assert "интерактивном терминале" in capsys.readouterr().out
    assert db.scalar(select(User).where(User.username == "root")) is None


def test_serve_uses_host_and_port(monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "8080"]) == 0
    assert calls == [("app.main:app", {"host": "0.0.0.0", "port": 8080, "proxy_headers": True,
                                         "ws_max_size": 1024 * 1024})]
    cli.main(["serve"])
    assert calls[1][1]["host"] == cli.config.host  # по умолчанию — из .env (HOST)


def test_create_admin_rejects_bad_input(monkeypatch, db):
    assert _run(monkeypatch, "root", ["short", "short"]) == 1
    assert _run(monkeypatch, "root", ["very-long-pass", "other-long-pass"]) == 1
    assert _run(monkeypatch, "admin", ["very-long-pass", "very-long-pass"]) == 1  # уже существует
    assert db.scalar(select(User).where(User.username == "root")) is None
