"""Настройки агента полями: из формы получаются те же разделы, что на узле; незнакомое не стирается; ошибки понятные."""
import pytest

from app.services import agent_form as af
from app.services.agent_remote import RemoteError


class Form:
    def __init__(self, items):
        self.items = items

    def multi_items(self):
        return self.items


CUR = {"interval": 10,
       "collectors": {"cpu": {}, "disks": {"include_mounts": ["/"], "proc_root": "/x"}, "mystery": {"a": 1}},
       "display": {"enabled": False, "address": 60, "night_start": "23:00", "foo": 1},
       "wol": {}, "update": {"enabled": True, "confirm_seconds": 120}}


def _submit(view, drop=(), add=()):
    items = []
    for s in view:
        items.append(("has_" + s["name"], "1"))
        if s["name"] != "interval":
            items.append(("extra_" + s["name"], s["extra"]))
        items += [(r["f"].name, r["value"]) for r in s["rows"] if r["present"] and r["f"].name not in drop]
    return Form(items + list(add))


def test_round_trip_keeps_values_and_unknown_keys():
    assert af.parse(_submit(af.view(list(CUR), CUR))) == CUR


def test_remove_collector_drops_its_params_add_field():
    out = af.parse(_submit(af.view(list(CUR), CUR), drop={"f.collectors.disks"},
                           add=[("f.collectors.systemd", "1"), ("f.collectors.systemd.units", "nginx.service, docker.service")]))
    assert "disks" not in out["collectors"] and out["collectors"]["systemd"] == {"units": ["nginx.service", "docker.service"]}
    assert out["collectors"]["mystery"] == {"a": 1}  # незнакомый сборщик — как был


@pytest.mark.parametrize("field,value,msg", [
    ("f.interval", "0", "1–3600"), ("f.interval", "abc", "число"), ("f.display.address", "zz", "0x3C"),
    ("f.display.night_end", "25:00", "ЧЧ:ММ"), ("f.collectors.checks.tcp", "{bad", "образец"),
    ("f.collectors.checks.tcp", '{"name": "x"}', "список"), ("f.wol.port", "22", "недопустимое"),
])
def test_bad_values_explained(field, value, msg):
    with pytest.raises(RemoteError, match=msg):
        af.parse(Form([(field, value)]))


def test_only_managed_sections():
    with pytest.raises(RemoteError):
        af.parse(Form([("has_panel", "1")]))
    with pytest.raises(RemoteError):
        af.parse(Form([("f.wol.enabled", "1")]), managed=["interval"])
    with pytest.raises(RemoteError, match="Нечего"):
        af.parse(Form([]))


def test_terminal_section_without_target():
    out = af.parse(Form([("has_terminal", "1"), ("f.terminal.enabled", "1"), ("f.terminal.max_sessions", "3")]))
    assert out == {"terminal": {"enabled": True, "max_sessions": 3}}
    with pytest.raises(RemoteError, match="только на узле"):
        af.parse(Form([("has_terminal", "1"), ("extra_terminal", '{"target": "10.0.0.5:22"}')]))
