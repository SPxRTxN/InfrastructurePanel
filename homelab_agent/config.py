"""Загрузка и проверка конфига узла (TOML, по умолчанию /etc/homelab-agent/agent.toml)."""
from __future__ import annotations

import os
import socket
import stat
import tomllib
from dataclasses import dataclass, field

import logging

from . import guard

log = logging.getLogger(__name__)

# Linux: одна папка /opt/homelab-agent (см. guard.py); запасной путь — раскладка до единой папки
LEGACY_PATH = (os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "homelab-agent", "agent.toml")
               if os.name == "nt" else f"{guard.LEGACY_ETC_DIR}/agent.toml")
DEFAULT_PATH = LEGACY_PATH if os.name == "nt" else f"{guard.ETC_DIR}/agent.toml"
TOP_KEYS = {"node", "interval", "buffer", "panel", "collectors", "actions", "display", "vpn", "wol", "update",
            "remote", "terminal"}


def default_path() -> str:
    """DEFAULT_PATH, а если его нет, но есть файл старой раскладки — он (до переноса install.sh --upgrade)."""
    if not os.path.exists(DEFAULT_PATH) and os.path.exists(LEGACY_PATH):
        return LEGACY_PATH
    return DEFAULT_PATH
PANEL_KEYS = {"url", "token_file", "token", "ca_file", "insecure", "buffer", "name"}


class ConfigError(ValueError):
    """Ошибка конфига с понятным текстом для владельца."""


@dataclass
class PanelConfig:
    url: str
    token_file: str | None = None
    token: str | None = field(default=None, repr=False)  # repr=False — не попадёт в логи
    ca_file: str | None = None
    insecure: bool = False
    buffer: int = 360
    name: str = ""

    def read_token(self) -> str:
        if self.token_file:
            with open(self.token_file, encoding="utf-8") as f:
                return f.read().strip()
        return (self.token or "").strip()


@dataclass
class AgentConfig:
    node: str
    interval: float = 10
    buffer: int = 360
    panels: list[PanelConfig] = field(default_factory=list)
    collectors: dict | None = None  # None → набор по умолчанию
    actions: dict = field(default_factory=dict)
    display: dict = field(default_factory=dict)
    vpn: dict = field(default_factory=dict)
    wol: dict = field(default_factory=dict)
    update: dict = field(default_factory=dict)
    remote: dict = field(default_factory=lambda: {"config": True, "uninstall": True})
    terminal: dict = field(default_factory=dict)  # туннель к локальному sshd; панель меняет всё, кроме target
    path: str | None = None


def _panel(raw: dict, idx: int, default_buffer: int) -> PanelConfig:
    where = f"[[panel]] №{idx + 1}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: должна быть таблицей")
    unknown = set(raw) - PANEL_KEYS
    if unknown:
        raise ConfigError(f"{where}: неизвестные ключи: {', '.join(sorted(unknown))}")
    url = raw.get("url")
    if not isinstance(url, str) or not url:
        raise ConfigError(f"{where}: нет url")
    insecure = raw.get("insecure", False)
    if not isinstance(insecure, bool):
        raise ConfigError(f"{where}: insecure — true/false")
    if url.startswith("ws://"):
        if not insecure:
            raise ConfigError(f"{where}: ws:// без шифрования запрещён; используйте wss:// (или insecure = true для локальной отладки)")
    elif not url.startswith("wss://"):
        raise ConfigError(f"{where}: url должен начинаться с wss://")
    token_file, token = raw.get("token_file"), raw.get("token")
    if bool(token_file) == bool(token):
        raise ConfigError(f"{where}: укажите ровно одно из token_file или token")
    buf = raw.get("buffer", default_buffer)
    if not isinstance(buf, int) or not 0 <= buf <= 100_000:
        raise ConfigError(f"{where}: buffer — целое 0..100000")
    return PanelConfig(
        url=url,
        token_file=str(token_file) if token_file else None,
        token=str(token) if token else None,
        ca_file=raw.get("ca_file"),
        insecure=insecure,
        buffer=buf,
        name=str(raw.get("name") or url.split("/")[2]),
    )


def parse_config(data: dict, path: str | None = None) -> AgentConfig:
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise ConfigError(f"неизвестные ключи верхнего уровня: {', '.join(sorted(unknown))}")
    node = data.get("node") or socket.gethostname()
    if not isinstance(node, str):
        raise ConfigError("node — строка")
    interval = data.get("interval", 10)
    if not isinstance(interval, (int, float)) or not 1 <= interval <= 3600:
        raise ConfigError("interval — число секунд 1..3600")
    buf = data.get("buffer", 360)
    if not isinstance(buf, int) or buf < 0:
        raise ConfigError("buffer — неотрицательное целое")
    panels_raw = data.get("panel", [])
    if isinstance(panels_raw, dict):
        panels_raw = [panels_raw]
    panels = [_panel(p, i, buf) for i, p in enumerate(panels_raw)]
    for sect in ("collectors", "actions", "display", "vpn", "wol", "update", "remote", "terminal"):
        if sect in data and not isinstance(data[sect], dict):
            raise ConfigError(f"[{sect}] должна быть таблицей")
    return AgentConfig(
        node=node,
        interval=float(interval),
        buffer=buf,
        panels=panels,
        collectors=data.get("collectors"),
        actions=data.get("actions", {}),
        display=data.get("display", {}),
        vpn=data.get("vpn", {}),
        wol=data.get("wol", {}),
        update=data.get("update", {}),
        remote=_remote(data.get("remote")),
        terminal=data.get("terminal", {}),
        path=path,
    )


def _remote(raw) -> dict:
    from .remote_config import parse_remote

    try:
        return parse_remote(raw)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def load_config(path: str | None = None) -> AgentConfig:
    """agent.toml, поверх — managed-разделы из соседнего panel.toml (настройки из панели; нет файла — как раньше)."""
    from . import remote_config

    path = path or default_path()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"нет файла конфига {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: ошибка TOML: {exc}") from None
    cfg = parse_config(data, path)
    over = remote_config.read_panel_toml(remote_config.panel_path(path))
    if over:
        try:
            merged = parse_config(remote_config.merge(data, over), path)
            remote_config.validate(merged)
            return merged
        except (ConfigError, ValueError, TypeError) as exc:
            # Испорченные настройки из панели не должны класть агента: работаем по agent.toml
            log.warning("panel.toml отвергнут, использую только agent.toml: %s", exc)
    return cfg


def _private(path: str) -> str | None:
    """None, если файл доступен только владельцу; иначе текст проблемы."""
    try:
        st = os.stat(path)
    except OSError as exc:
        return f"{path}: {exc.strerror}"
    # В Windows биты режима ничего не говорят о доступе: права задаёт ACL (start-agent.ps1 через icacls)
    if os.name != "nt" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path}: права {oct(st.st_mode & 0o777)}, нужно 0600 (chmod 600 {path})"
    return None


def check_config(cfg: AgentConfig) -> list[str]:
    """Проверки для `--check-config`: файлы токенов, права, сборка коллекторов и действий."""
    problems: list[str] = []
    from .actions import parse_actions
    from .collectors import build_collectors

    try:
        build_collectors(cfg.collectors, cfg.interval)
    except ValueError as exc:
        problems.append(str(exc))
    try:
        parse_actions(cfg.actions)
    except ValueError as exc:
        problems.append(str(exc))
    try:
        from .vpn import parse_vpn

        vpn_cfg = parse_vpn(cfg.vpn)
        # нет sing-box, но есть помощник — агент поставит его сам при первом конфиге от панели
        if vpn_cfg.enabled and not os.path.exists(vpn_cfg.singbox) and not os.path.exists(vpn_cfg.setup):
            problems.append(f"[vpn]: не найден sing-box ({vpn_cfg.singbox}) — установите или укажите singbox = \"…\"")
    except ValueError as exc:
        problems.append(str(exc))
    try:
        from .wol import parse_wol

        parse_wol(cfg.wol)
    except (ValueError, TypeError) as exc:
        problems.append(str(exc))
    try:
        from .tunnel import parse_terminal

        parse_terminal(cfg.terminal)
    except (ValueError, TypeError) as exc:
        problems.append(str(exc))
    try:
        from .updater import check_update

        problems.extend(check_update(cfg.update))
    except ValueError as exc:
        problems.append(str(exc))
    inline = False
    for p in cfg.panels:
        if p.token_file:
            issue = _private(p.token_file)
            if issue:
                problems.append(f"токен панели {p.name}: {issue}")
            else:
                try:
                    if not p.read_token():
                        problems.append(f"токен панели {p.name}: файл {p.token_file} пуст")
                except OSError as exc:
                    problems.append(f"токен панели {p.name}: {exc.strerror}")
        else:
            inline = True
        if p.ca_file and not os.path.isfile(p.ca_file):
            problems.append(f"панель {p.name}: нет ca_file {p.ca_file}")
    if inline and cfg.path:
        issue = _private(cfg.path)
        if issue:
            problems.append(f"в конфиге есть token: {issue}")
    if cfg.display.get("enabled"):
        from .display.base import check_display_config

        problems.extend(check_display_config(cfg.display))
    return problems
