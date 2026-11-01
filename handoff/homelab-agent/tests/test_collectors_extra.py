import http.server
import os
import socket
import threading

import pytest

from conftest import fixture_text, make_tree
from homelab_agent.collectors import build_collectors
from homelab_agent.collectors.checks import ChecksCollector
from homelab_agent.collectors.command import CommandCollector, parse_output
from homelab_agent.collectors.processes import ProcessesCollector
from homelab_agent.collectors.systemd import SystemdCollector


def proc_entry(pid, comm, cmdline, utime, stime, rss_pages):
    # Поля после «)»: state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt utime stime ...
    stat = f"{pid} ({comm}) S 1 {pid} {pid} 0 -1 4194560 100 0 0 0 {utime} {stime} 0 0 20 0 1 0 100 0 0\n"
    return {
        f"{pid}/cmdline": "\0".join(cmdline) + "\0",
        f"{pid}/stat": stat,
        f"{pid}/statm": f"5000 {rss_pages} 300 10 0 400 0\n",
    }


@pytest.fixture
def proc_tree(tmp_path):
    files = {}
    files.update(proc_entry(100, "nginx", ["nginx: master process /usr/sbin/nginx"], 50, 50, 1000))
    files.update(proc_entry(101, "nginx", ["nginx: worker process"], 500, 100, 2000))
    files.update(proc_entry(200, "my proc) x", ["/usr/local/bin/xray", "run", "-c", "/etc/xray.json"], 10, 0, 300))
    files["2/stat"] = "2 (kthreadd) S 0 0 0 0 -1 0 0 0 0 0 0 0 0 0\n"
    files["2/cmdline"] = ""
    files["2/statm"] = "0 0 0 0 0 0 0\n"
    make_tree(tmp_path / "proc", files)
    return tmp_path / "proc"


def test_processes(proc_tree):
    c = ProcessesCollector({"proc_root": str(proc_tree), "watch": [
        {"name": "nginx", "match": "nginx: master"},
        {"name": "xray", "match": "xray"},
        {"name": "absent", "match": "nosuchthing"},
    ]})
    page = os.sysconf("SC_PAGE_SIZE")
    v = c.read()
    assert v["processes.nginx.running"] == 1 and v["processes.nginx.rss"] == 1000 * page
    assert v["processes.xray.running"] == 1 and v["processes.xray.rss"] == 300 * page
    assert v["processes.absent.running"] == 0 and v["processes.absent.rss"] == 0
    assert "processes.nginx.cpu" not in v  # CPU — только со второго чтения
    tck = os.sysconf("SC_CLK_TCK")
    make_tree(proc_tree, proc_entry(100, "nginx", ["nginx: master process /usr/sbin/nginx"], 50 + tck, 50, 1000))
    v2 = c.read()
    assert v2["processes.nginx.cpu"] > 0
    assert v2["processes.xray.cpu"] == 0
    ids = {s.id for s in c.sensors()}
    assert {"processes.nginx.running", "processes.nginx.cpu", "processes.nginx.rss"} <= ids


@pytest.mark.parametrize("watch", [
    [{"name": "Bad Name", "match": "x"}],
    [{"name": "a"}],
    [{"name": "a", "match": "x", "extra": 1}],
    [{"name": "a", "match": "x"}, {"name": "a", "match": "y"}],
    "nginx",
])
def test_processes_validation(watch):
    with pytest.raises(ValueError):
        ProcessesCollector({"watch": watch})


def test_systemd_parses_is_active():
    calls = []

    def fake(argv, timeout):
        calls.append(argv)
        return 3, fixture_text("systemctl_is_active.txt"), ""

    c = SystemdCollector({"units": ["nginx.service", "docker.service", "xray.service", "caddy.service"]})
    c.run_argv = fake
    assert c.read() == {
        "systemd.nginx.service.active": 1,
        "systemd.docker.service.active": 0,
        "systemd.xray.service.active": 0,
        "systemd.caddy.service.active": 1,
    }
    assert calls == [["systemctl", "is-active", "--", "nginx.service", "docker.service", "xray.service",
                      "caddy.service"]]
    assert [s.kind for s in c.sensors()] == ["state"] * 4


def test_systemd_validation_and_mismatch():
    with pytest.raises(ValueError):
        SystemdCollector({"units": ["nginx; rm -rf /"]})
    with pytest.raises(ValueError):
        SystemdCollector({"units": ["nginx"]})  # без суффикса
    c = SystemdCollector({"units": ["a.service", "b.service"]})
    c.run_argv = lambda argv, timeout: (0, "active\n", "")
    assert c.read() == {}


def test_checks_tcp_and_http():
    # Локальный TCP-сервер и HTTP-сервер на 127.0.0.1 со случайным портом
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    tcp_port = srv.getsockname()[1]
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    hp = httpd.server_address[1]
    try:
        c = ChecksCollector({
            "tcp": [{"name": "open", "host": "127.0.0.1", "port": tcp_port},
                    {"name": "closed", "host": "127.0.0.1", "port": closed_port, "timeout": 1}],
            "http": [{"name": "health", "url": f"http://127.0.0.1:{hp}/health"},
                     {"name": "missing", "url": f"http://127.0.0.1:{hp}/nope"},
                     {"name": "notfound", "url": f"http://127.0.0.1:{hp}/nope", "expect": 404}],
        })
        v = c.read()
    finally:
        httpd.shutdown()
        srv.close()
    assert v["checks.open.up"] == 1 and v["checks.open.latency_ms"] >= 0
    assert v["checks.closed.up"] == 0 and "checks.closed.latency_ms" not in v
    assert v["checks.health.up"] == 1 and v["checks.missing.up"] == 0 and v["checks.notfound.up"] == 1


def test_checks_validation():
    with pytest.raises(ValueError):
        ChecksCollector({"tcp": [{"name": "x", "host": "h", "port": 0}]})
    with pytest.raises(ValueError):
        ChecksCollector({"http": [{"name": "x", "url": "file:///etc/passwd"}]})
    with pytest.raises(ValueError):
        ChecksCollector({"tcp": [{"name": "x", "host": "h", "port": 1}], "http": [{"name": "x", "url": "http://h"}]})


@pytest.mark.parametrize("mode, code, out, err, expected", [
    ("first_int", 0, "", "12;3", 12),
    ("first_int", 0, "updates: 7\n", "", 7),
    ("first_float", 0, "temp=48.3'C\n", "", 48.3),
    ("lines", 0, "a\n\nb\nc\n", "", 3),
    ("exit_code", 2, "", "", 2),
    ("first_int", 0, "нет чисел", "", None),
])
def test_command_parse(mode, code, out, err, expected):
    assert parse_output(mode, code, out, err) == expected


def test_command_intervals_and_no_shell():
    calls = []

    def fake(argv, timeout):
        calls.append((argv, timeout))
        return 0, "", "12;0"

    c = CommandCollector({"items": [
        {"name": "apt_updates", "argv": ["/usr/lib/update-notifier/apt-check", "--quiet"], "interval": 3600},
        {"name": "fast", "argv": ["/bin/echo", "5"], "parse": "first_int", "interval": 5, "timeout": 2},
    ]})
    c.run_argv = fake
    assert c.interval == 5
    assert c.read() == {"command.apt_updates.value": 12, "command.fast.value": 12}
    assert c.read() == {}  # ещё не пора
    assert calls[0] == (["/usr/lib/update-notifier/apt-check", "--quiet"], 10.0)
    assert calls[1] == (["/bin/echo", "5"], 2.0)


def test_command_real_subprocess_without_shell():
    c = CommandCollector({"items": [{"name": "echo", "argv": ["echo", "$((40+2)); 7"], "parse": "first_int"}]})
    # Без shell подстановка не выполняется: первое число в выводе — 40
    assert c.read() == {"command.echo.value": 40}


def test_command_validation():
    for items in ([{"name": "x", "argv": []}], [{"name": "x", "argv": "ls -l"}],
                  [{"name": "x", "argv": ["ls"], "parse": "eval"}], [{"name": "x", "argv": ["ls"], "kind": "Bad"}]):
        with pytest.raises(ValueError):
            CommandCollector({"items": items})


def test_example_config_collectors_build():
    import tomllib

    here = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(here, "agent.full.toml"), "rb") as f:
        data = tomllib.load(f)
    names = [c.name for c in build_collectors(data["collectors"], 10)]
    assert {"processes", "systemd", "checks", "command"} <= set(names)
