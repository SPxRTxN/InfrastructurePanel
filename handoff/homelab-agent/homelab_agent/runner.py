"""Главный цикл агента: расписание коллекторов, рассылка выборок во все панели."""
from __future__ import annotations

import asyncio
import logging
import socket
import time

from . import protocol, remote_config, sysinfo
from .actions import ActionRunner, AutoRecovery, parse_actions
from .collectors import build_collectors, safe_read
from .config import AgentConfig
from .transport import PanelClient
from .updater import Updater, parse_update, run_cmd
from .tunnel import TunnelManager, parse_terminal
from .vpn import VpnManager, parse_vpn
from .vpn import info as vpn_info
from .wol import WolSender, parse_wol

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, cfg: AgentConfig, *, collectors=None, client_kwargs: dict | None = None):
        self.cfg = cfg
        self.collectors = collectors if collectors is not None else build_collectors(cfg.collectors, cfg.interval)
        self.latest: dict[str, float] = {}  # последние значения — для дисплея и правил
        self.started = time.time()
        specs = parse_actions(cfg.actions)
        self.actions = ActionRunner(specs)
        self.auto = AutoRecovery(specs)
        self._bg: set[asyncio.Task] = set()
        self.display_cfg = None
        if cfg.display.get("enabled"):
            from .display.base import parse_display

            self.display_cfg = parse_display(cfg.display)
        self._net: tuple[float, str, str] = (0.0, "", "")  # (когда, IP, интерфейс) — кэш для дисплея
        vpn_cfg = parse_vpn(cfg.vpn)
        self.vpn_cfg = vpn_cfg
        self.vpn = VpnManager(vpn_cfg, [p.url for p in cfg.panels]) if vpn_cfg.enabled else None
        wol_cfg = parse_wol(cfg.wol)
        self.wol = WolSender(wol_cfg) if wol_cfg.enabled else None
        term_cfg = parse_terminal(cfg.terminal)
        self.tunnels = TunnelManager(term_cfg) if term_cfg.enabled else None
        upd_cfg = parse_update(cfg.update)
        self.updater = Updater(upd_cfg, cfg.path) if upd_cfg.enabled else None
        # Настройки и удаление из панели: только при новой раскладке и если владелец не выключил в [remote]
        self.remote_features = [f for f in ("config", "uninstall")
                                if remote_config.layout_ok(cfg.path) and cfg.remote.get(f, True)]
        self.run_cmd = run_cmd  # подменяется в тестах
        self.launch_uninstall = remote_config.launch_uninstall
        self.remote_delay = 1.0  # пауза, чтобы ответ дошёл до панели раньше перезапуска
        self.clients = [
            PanelClient(p, self.hello, self.handle_action, on_vpn=self.vpn.handle if self.vpn else None,
                        on_wol=self.wol.handle if self.wol else None,
                        on_update=self.handle_update if self.updater else None,
                        on_remote=self.handle_remote, tunnels=self.tunnels,
                        **(client_kwargs or {})) for p in cfg.panels
        ]

    # --- протокол ---
    def sensors(self) -> list[dict]:
        out, seen = [], set()
        for c in self.collectors:
            try:
                for s in c.sensors():
                    if s.id not in seen:
                        seen.add(s.id)
                        out.append(s.as_dict())
            except Exception as exc:  # noqa: BLE001
                log.warning("коллектор %s: ошибка описания датчиков: %s", c.name, exc)
        return out

    def hello(self, token: str) -> dict:
        msg = protocol.hello(self.cfg.node, token, self.sensors(), self.actions.describe(), sysinfo.collect())
        # Возможности, включённые владельцем: панель шлёт сообщения vpn только тем, кто их объявил
        msg.setdefault("info", {})["vpn"] = vpn_info(self.vpn_cfg)
        msg["features"] = [name for name, on in (("vpn", self.vpn), ("wol", self.wol), ("update", self.updater)) if on]
        msg["features"] += self.remote_features
        if self.tunnels:
            msg["features"].append("tunnel")
            msg["tunnel_max"] = self.tunnels.cfg.max_sessions
        return msg

    async def handle_remote(self, client: PanelClient, msg: dict) -> None:
        """config_get / config_set / uninstall. Присланное — данные; запускается только фиксированный скрипт."""
        kind = msg.get("type")
        uid = msg.get("id") if isinstance(msg.get("id"), str) and len(msg["id"]) <= 64 else ""
        feature = "uninstall" if kind == "uninstall" else "config"
        result_type = "uninstall_result" if kind == "uninstall" else "config_result"
        if feature not in self.remote_features:
            client.send_control({"type": result_type, "id": uid, "ok": False,
                                 "detail": "на узле выключено или не поддерживается этой раскладкой"})
            return
        if kind == "config_get":
            client.send_control(remote_config.config_message(uid, self.cfg))
        elif kind == "config_set":
            try:
                detail = await asyncio.to_thread(remote_config.apply_config_set, self.cfg.path, msg.get("sections"))
            except remote_config.RemoteError as exc:
                client.send_control({"type": "config_result", "id": uid, "ok": False, "detail": str(exc)[:500]})
                return
            client.send_control({"type": "config_result", "id": uid, "ok": True, "detail": detail})
            await client.flush()
            await asyncio.sleep(self.remote_delay)
            restart = parse_update(self.cfg.update).restart_argv
            code = (await asyncio.to_thread(self.run_cmd, restart, None, 30))[0]
            if code:
                self.broadcast_event("warning", f"настройки сохранены, но перезапуск не удался (код {code})")
        else:
            client.send_control({"type": "uninstall_result", "id": uid, "ok": True,
                                 "detail": "удаление запускается"})
            await client.flush()
            await asyncio.sleep(self.remote_delay)
            code = await asyncio.to_thread(self.launch_uninstall)
            if code:
                self.broadcast_event("warning", f"удаление не запустилось (код {code}): нет правила sudo?")

    async def handle_update(self, client: PanelClient, msg: dict) -> None:
        """update от панели: проверить, переключить версию, ответить и только потом перезапуститься."""
        result = await self.updater.handle(msg, client.cfg)
        client.send_control(result)
        if result["ok"] and self.updater.restart_pending:
            await client.flush()
            await asyncio.sleep(1)  # дать ответу дойти до панели
            code = await asyncio.to_thread(self.updater.restart)
            if code:
                self.broadcast_event("warning", f"обновление: версия включена, но перезапуск не удался (код {code}); "
                                                "новая версия начнёт работать после ручного перезапуска, иначе сторож откатит")

    async def handle_action(self, action_id: str, name: str) -> dict:
        return await self.actions.run(action_id, name, source="panel")

    def broadcast_event(self, level: str, text: str) -> None:
        msg = protocol.event(int(time.time() * 1000), level, text)
        for client in self.clients:
            client.send_control(msg)

    # --- сбор ---
    def publish(self, values: dict, ts_ms: int | None = None) -> None:
        if not values:
            return
        self.latest.update(values)
        msg = protocol.sample(ts_ms or int(time.time() * 1000), values)
        for client in self.clients:
            client.publish(msg)

    async def _scheduler(self, stop: asyncio.Event) -> None:
        next_due = {id(c): 0.0 for c in self.collectors}
        while not stop.is_set():
            now = time.monotonic()
            due = [c for c in self.collectors if next_due[id(c)] <= now]
            if due:
                values: dict = {}
                for c in due:
                    # Чтение в потоке: проверки и команды могут ждать секунды
                    values.update(await asyncio.to_thread(safe_read, c))
                    next_due[id(c)] = now + c.interval
                self.publish(values)
                await self.after_sample(values)
            if not self.collectors:
                await stop.wait()
                return
            sleep = max(0.05, min(next_due.values()) - time.monotonic())
            try:
                await asyncio.wait_for(stop.wait(), timeout=sleep)
            except asyncio.TimeoutError:
                pass

    async def after_sample(self, values: dict) -> None:
        """Правила автовосстановления: запуск в фоне, чтобы не задерживать сбор."""
        for name in self.auto.due(self.latest):
            task = asyncio.create_task(self._auto_run(name))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)

    async def _auto_run(self, name: str) -> None:
        spec = self.actions.specs[name]
        log.warning("автовосстановление: %s (условие «%s» держится %d с)", name, spec.auto.when, spec.auto.hold)
        res = await self.actions.run(f"auto-{int(time.time())}", name, source="auto")
        if res.get("error") in ("busy", "rate limited"):
            self.broadcast_event("warning", f"{spec.label}: автозапуск пропущен ({res['error']}), условие «{spec.auto.when}»")
        else:
            level = "warning" if res["ok"] else "error"
            outcome = "выполнено" if res["ok"] else f"ошибка (код {res['code']}, {res.get('error') or 'см. журнал'})"
            self.broadcast_event(level, f"{spec.label}: автовосстановление по условию «{spec.auto.when}» — {outcome}")

    # --- дисплей ---
    def display_state(self):
        from .display.base import DisplayState

        now = time.time()
        if now - self._net[0] > 60:  # IP может смениться (DHCP), но не чаще раза в минуту
            _mac, ips, iface = sysinfo.network()
            self._net = (now, ips[0] if ips else "", iface or "")
        summary = next((c.summary for c in self.clients if c.summary), None)
        return DisplayState(
            hostname=socket.gethostname(),
            ip=self._net[1],
            iface=self._net[2],
            values=dict(self.latest),
            panels=[(c.name, c.connected) for c in self.clients],
            summary=summary,
            last_action=self.actions.last,
            agent_started=self.started,
        )

    def _display_task(self, stop: asyncio.Event):
        if self.display_cfg is None:
            return None
        from .display.base import Display
        from .display.ssd1306 import create_device

        device = create_device(self.display_cfg)
        if device is None:
            return None  # предупреждение уже в журнале, агент работает дальше
        return asyncio.create_task(Display(device, self.display_cfg, self.display_state).run(stop), name="display")

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._scheduler(stop), name="scheduler")]
        tasks += [asyncio.create_task(c.run(stop), name=f"panel:{c.name}") for c in self.clients]
        display = self._display_task(stop)
        if display:
            tasks.append(display)
        log.info("агент запущен: узел %s, коллекторов %d, панелей %d",
                 self.cfg.node, len(self.collectors), len(self.clients))
        await asyncio.gather(*tasks)
