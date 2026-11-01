import json
import os

import pytest

from homelab_agent.__main__ import main
from homelab_agent.config import ConfigError, check_config, load_config, parse_config


def write(path, text, mode=0o600):
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_defaults():
    cfg = parse_config({"node": "pi"})
    assert cfg.node == "pi" and cfg.interval == 10 and cfg.panels == [] and cfg.collectors is None


def test_hostname_default():
    import socket

    assert parse_config({}).node == socket.gethostname()


@pytest.mark.parametrize("panel, msg", [
    ({"url": "ws://x/agent/ws", "token": "t"}, "ws://"),
    ({"url": "http://x/agent/ws", "token": "t"}, "wss://"),
    ({"url": "wss://x/agent/ws"}, "ровно одно"),
    ({"url": "wss://x/agent/ws", "token": "t", "token_file": "/f"}, "ровно одно"),
    ({"url": "wss://x/agent/ws", "token": "t", "oops": 1}, "неизвестные"),
])
def test_panel_validation(panel, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config({"panel": [panel]})


def test_ws_allowed_only_with_insecure():
    cfg = parse_config({"panel": [{"url": "ws://127.0.0.1:9/agent/ws", "token": "t", "insecure": True}]})
    assert cfg.panels[0].insecure


def test_token_not_in_repr():
    cfg = parse_config({"panel": [{"url": "wss://x/agent/ws", "token": "SECRET123"}]})
    assert "SECRET123" not in repr(cfg)


def test_unknown_top_key():
    with pytest.raises(ConfigError, match="неизвестные ключи"):
        parse_config({"nodes": "x"})


def test_load_example_config():
    here = os.path.dirname(os.path.dirname(__file__))
    cfg = load_config(os.path.join(here, "agent.full.toml"))
    assert len(cfg.panels) == 2
    assert cfg.panels[0].url.startswith("wss://")
    cfg = load_config(os.path.join(here, "agent.example.toml"))
    assert cfg.node == "raspberry-pi" and len(cfg.panels) == 1


def test_check_config_token_permissions(tmp_path):
    tok = write(tmp_path / "tok", "abc\n", 0o644)
    conf = write(tmp_path / "agent.toml", f'node = "n"\n[[panel]]\nurl = "wss://p/agent/ws"\ntoken_file = "{tok}"\n')
    cfg = load_config(str(conf))
    problems = check_config(cfg)
    assert any("0600" in p for p in problems)
    os.chmod(tok, 0o600)
    assert check_config(cfg) == []
    assert cfg.panels[0].read_token() == "abc"
    assert main(["-c", str(conf), "--check-config"]) == 0


def test_check_config_inline_token_needs_private_config(tmp_path):
    conf = write(tmp_path / "agent.toml", '[[panel]]\nurl = "wss://p/agent/ws"\ntoken = "x"\n', 0o644)
    assert any("token" in p for p in check_config(load_config(str(conf))))


def test_once_outputs_json(tmp_path, pi_root, capsys, monkeypatch):
    monkeypatch.setattr("homelab_agent.__main__.time.sleep", lambda s: None)
    conf = write(tmp_path / "agent.toml", (
        'node = "pi"\n[collectors.memory]\nproc_root = "{p}"\n'
        '[collectors.thermal]\nsys_root = "{s}"\n'
    ).format(p=pi_root["proc_root"], s=pi_root["sys_root"]))
    assert main(["-c", str(conf), "--once"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["node"] == "pi"
    assert out["values"]["thermal.cpu.temp"] == 51.2
    assert out["values"]["memory.total"] == 945904 * 1024
    assert {s["id"] for s in out["sensors"]} >= {"memory.used", "thermal.cpu.temp"}


def test_bad_config_exit_code(tmp_path):
    conf = write(tmp_path / "agent.toml", "interval = 0\n")
    assert main(["-c", str(conf), "--once"]) == 2
