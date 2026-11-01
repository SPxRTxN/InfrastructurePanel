"""Оповещения: правила → наблюдения → состояние (ожидание / горит / прошло) → доставка.

Каждые 30 с задача `alerts` вызывает `evaluate`. Для каждого правила собираются наблюдения по объектам
(узел, сервер, раздел диска, датчик): «условие выполнено / нет» и значение. Условие должно держаться
`for_minutes`, тогда правило «горит» и уходит оповещение; когда условие пропадает — «прошло».
Тишина (`alerts.silence_until`) не останавливает учёт, только доставку.

Каналы: колокольчик (всем с `servers.view` или только группам правила, оттуда и браузерные уведомления), Telegram, webhook (ntfy/JSON).
Токен бота хранится зашифрованным (Fernet, SECRET_KEY) и в UI не показывается.
"""
import asyncio
import base64
import json
import logging
import operator
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AlertLog, AlertRule, AlertState, Node, NodeEvent, Notification, Server, Service, User
from app.services import agents, monitoring
from app.services.security import decrypt
from app.services.settings_registry import get_setting
from app.utils import utcnow

log = logging.getLogger(__name__)

KINDS = {
    "node_offline": "Узел с агентом не на связи",
    "agent_restarted": "Агент переподключился (обрыв дольше N мин или новая версия)",
    "server_offline": "Сервер офлайн",
    "service_down": "Сервис недоступен",
    "disk": "Диск заполнен, %",
    "temperature": "Температура, °C",
    "sensor": "Датчик агента",
}
NEEDS_THRESHOLD = {"disk", "temperature", "sensor"}
OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq, "!=": operator.ne}
TIMEOUT = 10
RESTART_WINDOW = timedelta(minutes=30)  # сколько событие «агент вернулся» остаётся наблюдением


@dataclass
class Observation:
    key: str
    title: str
    link: str
    active: bool
    value: str = ""


def default_rules() -> list[AlertRule]:
    return [
        AlertRule(name="Узел не на связи", kind="node_offline", for_minutes=3, severity="critical"),
        AlertRule(name="Сервер офлайн", kind="server_offline", for_minutes=3, severity="critical"),
        AlertRule(name="Сервис недоступен", kind="service_down", for_minutes=3),
        AlertRule(name="Диск заполнен", kind="disk", threshold=90, for_minutes=10),
        AlertRule(name="Перегрев", kind="temperature", threshold=75, for_minutes=5),
    ]


# ---------- Наблюдения ----------

def _match(rule: AlertRule, name: str) -> bool:
    return not rule.target or rule.target == name


def _fmt(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def observe(db: Session, rule: AlertRule) -> list[Observation]:
    out: list[Observation] = []
    if rule.kind == "node_offline":
        for n in db.scalars(select(Node)):
            if n.last_seen is None or not _match(rule, n.name):
                continue  # ни разу не подключался — это не «пропал»
            online = agents.is_online(db, n)
            minutes = int((utcnow() - n.last_seen).total_seconds() // 60)
            out.append(Observation(f"node:{n.id}", f"узел {n.name}", f"/nodes/{n.id}", not online,
                                   "" if online else f"{minutes} мин без связи"))
    elif rule.kind == "agent_restarted":
        # Каждое событие из handle_hello — отдельное наблюдение, пока оно свежее; порог — минимальный обрыв, мин
        since = utcnow() - RESTART_WINDOW
        min_gap = rule.threshold if rule.threshold is not None else 5
        for ev in db.scalars(select(NodeEvent).where(NodeEvent.text.startswith(agents.RESTART_MARK),
                                                     NodeEvent.ts >= since)):
            n = db.get(Node, ev.node_id)
            if not n or not _match(rule, n.name):
                continue
            gap = re.search(r"обрыв (\d+) мин", ev.text)
            active = "версия" in ev.text or (gap is not None and int(gap.group(1)) >= min_gap)
            out.append(Observation(f"node:{n.id}:restart:{ev.id}", f"агент на {n.name}", f"/nodes/{n.id}", active,
                                   ev.text.removeprefix(agents.RESTART_MARK)[:64]))
    elif rule.kind == "server_offline":
        for s in db.scalars(select(Server).where(Server.enabled.is_(True))):
            if s.online is None or not _match(rule, s.name):
                continue
            out.append(Observation(f"server:{s.id}", f"сервер {s.name}", f"/servers/{s.id}", s.online is False,
                                   s.last_error[:60] if s.online is False else ""))
    elif rule.kind == "service_down":
        for sv in db.scalars(select(Service)):
            if sv.ok is None or not _match(rule, sv.server.name):
                continue
            out.append(Observation(f"service:{sv.id}", f"сервис {sv.name} на {sv.server.name}",
                                   f"/servers/{sv.server_id}", sv.ok is False, sv.last_error[:60]))
    elif rule.kind == "disk":
        seen_nodes = set()
        for s in db.scalars(select(Server).where(Server.enabled.is_(True))):
            if not _match(rule, s.name) or not s.online:
                continue
            seen_nodes.update(n.id for n in db.scalars(select(Node).where(Node.server_id == s.id)))
            m = monitoring.latest_metric(db, s.id)
            for d in json.loads(m.disks) if m else []:
                out.append(_disk(rule, f"server:{s.id}", f"сервер {s.name}", f"/servers/{s.id}", d))
        for n in db.scalars(select(Node)):  # узлы без сервера — по данным агента
            if n.id in seen_nodes or not _match(rule, n.name) or not agents.is_online(db, n):
                continue
            for d in agents.server_metrics(n.values, n.sensors).disks:
                out.append(_disk(rule, f"node:{n.id}", f"узел {n.name}", f"/nodes/{n.id}", d))
    elif rule.kind in ("temperature", "sensor"):
        compare = OPS.get(rule.op, operator.ge) if rule.kind == "sensor" else operator.ge
        for n in db.scalars(select(Node)):
            if not _match(rule, n.name) or not agents.is_online(db, n):
                continue
            values = n.values
            for s in n.sensors:
                wanted = s["kind"] == "temperature" if rule.kind == "temperature" else s["id"] == rule.sensor
                if not wanted or s["id"] not in values or rule.threshold is None:
                    continue
                v = values[s["id"]]
                unit = " °C" if rule.kind == "temperature" else (f" {s['unit']}" if s.get("unit") else "")
                out.append(Observation(f"node:{n.id}:{s['id']}", f"{s['label']} на {n.name}", f"/nodes/{n.id}",
                                       compare(v, rule.threshold), _fmt(v) + unit))
    return out


def _disk(rule: AlertRule, owner: str, owner_title: str, link: str, d: dict) -> Observation:
    pct = d["used"] * 100 / d["size"] if d.get("size") else 0
    return Observation(f"{owner}:disk:{d['mount']}", f"диск {d['mount']} — {owner_title}", link,
                       rule.threshold is not None and pct >= rule.threshold, f"{pct:.0f} %")


# ---------- Состояние ----------

@dataclass
class Change:
    rule: AlertRule
    state: AlertState
    kind: str  # firing | repeat | resolved


def evaluate(db: Session, now: datetime | None = None) -> list[Change]:
    now = now or utcnow()
    changes: list[Change] = []
    for rule in db.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))):
        states = {s.key: s for s in db.scalars(select(AlertState).where(AlertState.rule_id == rule.id))}
        seen = set()
        for ob in observe(db, rule):
            seen.add(ob.key)
            st = states.get(ob.key)
            if st is None:
                st = AlertState(rule_id=rule.id, key=ob.key[:300])
                db.add(st)
                states[ob.key] = st
            st.title, st.link, st.value, st.seen_at = ob.title[:300], ob.link[:255], ob.value[:64], now
            if ob.active:
                st.pending_since = st.pending_since or now
                if not st.firing and now - st.pending_since >= timedelta(minutes=rule.for_minutes):
                    st.firing, st.fired_at, st.last_sent = True, now, now
                    changes.append(Change(rule, st, "firing"))
                elif st.firing and rule.repeat_minutes and st.last_sent \
                        and now - st.last_sent >= timedelta(minutes=rule.repeat_minutes):
                    st.last_sent = now
                    changes.append(Change(rule, st, "repeat"))
            else:
                st.pending_since = None
                if st.firing:
                    st.firing = False
                    if rule.kind != "agent_restarted":
                        changes.append(Change(rule, st, "resolved"))
        # Объект исчез (удалили сервер, отвязали узел): горящее — закрыть, остальное — забыть
        for key, st in states.items():
            if key not in seen:
                if st.firing:
                    st.firing = False
                    st.value = "объекта больше нет"
                    if rule.kind != "agent_restarted":  # разовое событие: «Нормы» после него не шлём
                        changes.append(Change(rule, st, "resolved"))
                db.delete(st)
    db.commit()
    return changes


def silenced(db: Session, now: datetime | None = None) -> datetime | None:
    raw = get_setting(db, "alerts.silence_until")
    try:
        until = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None
    return until if until and until > (now or utcnow()) else None


# ---------- Доставка ----------

def message(change: Change) -> tuple[str, str]:
    """(заголовок, текст) — одинаково для всех каналов."""
    st, rule = change.state, change.rule
    mark = {"firing": "Тревога", "repeat": "Всё ещё тревога", "resolved": "Норма"}[change.kind]
    title = f"{mark}: {rule.name} — {st.title}"
    body = st.value or ""
    if change.kind == "resolved" and st.fired_at:
        mins = max(1, int((utcnow() - st.fired_at).total_seconds() // 60))
        body = (body + "; " if body else "") + f"длилось {mins} мин"
    return title[:255], body[:1000]


def send_telegram(token: str, chat: str, text: str) -> str:
    """'' — отправлено, иначе текст ошибки (без токена)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=TIMEOUT):  # nosec B310 — фиксированный https
            return ""
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        return e.__class__.__name__


def send_webhook(url: str, kind: str, title: str, body: str, severity: str, state: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "нужен http:// или https://"
    if kind == "json":
        data = json.dumps({"title": title, "message": body, "severity": severity, "state": state},
                          ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
    else:  # ntfy: текст в теле, заголовок и приоритет — в заголовках
        data = (body or title).encode()
        prio = "5" if severity == "critical" and state != "resolved" else "3"
        headers = {"Title": _ascii_header(title), "Priority": prio,
                   "Tags": "white_check_mark" if state == "resolved" else "warning"}
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT):  # nosec B310 — адрес задаёт администратор, схема проверена
            return ""
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        return e.__class__.__name__


def _ascii_header(text: str) -> str:
    # Заголовки HTTP — latin-1; кириллицу ntfy принимает в виде RFC 2047
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode()).decode() + "?="


def channels(db: Session) -> dict:
    token_enc = get_setting(db, "alerts.telegram_token")
    return {"telegram_token": decrypt(token_enc) if token_enc else "",
            "telegram_chat": get_setting(db, "alerts.telegram_chat"),
            "webhook_url": get_setting(db, "alerts.webhook_url"),
            "webhook_kind": get_setting(db, "alerts.webhook_kind")}


def bell(db: Session, title: str, body: str, link: str, groups: list | None = None) -> None:
    """Колокольчик: активные пользователи с servers.view (оповещение ведёт на страницы раздела — без права
    ссылка бесполезна); если заданы группы правила — только члены этих групп."""
    wanted = {g.id for g in groups or []}
    for u in db.scalars(select(User).where(User.is_active.is_(True))):
        if u.can("servers.view") and (not wanted or any(g.id in wanted for g in u.groups)):
            db.add(Notification(user_id=u.id, title=title, body=body, link=link))


async def deliver(db: Session, changes: list[Change]) -> None:
    if not changes:
        return
    quiet = silenced(db)
    ch = channels(db)
    for c in changes:
        title, body = message(c)
        done: list[str] = []
        if quiet:
            done.append("тишина")
        else:
            if c.rule.notify_bell:
                bell(db, title, body, c.state.link, c.rule.groups)
                done.append("колокольчик")
            if c.rule.notify_telegram and ch["telegram_token"] and ch["telegram_chat"]:
                err = await asyncio.to_thread(send_telegram, ch["telegram_token"], ch["telegram_chat"],
                                              f"{title}\n{body}".strip())
                done.append("telegram" + (f": {err}" if err else ""))
            if c.rule.notify_webhook and ch["webhook_url"]:
                err = await asyncio.to_thread(send_webhook, ch["webhook_url"], ch["webhook_kind"], title, body,
                                              c.rule.severity, c.kind)
                done.append("webhook" + (f": {err}" if err else ""))
        db.add(AlertLog(rule_name=c.rule.name[:128], severity=c.rule.severity,
                        state="resolved" if c.kind == "resolved" else "firing", title=c.state.title[:300],
                        value=c.state.value[:64], link=c.state.link[:255], delivered=", ".join(done)[:100]))
    db.commit()


async def run(db: Session) -> None:
    await deliver(db, evaluate(db))


def firing(db: Session) -> list[tuple[AlertState, AlertRule]]:
    rows = db.execute(select(AlertState, AlertRule).join(AlertRule, AlertRule.id == AlertState.rule_id)
                      .where(AlertState.firing.is_(True)).order_by(AlertState.fired_at.desc())).all()
    return [(s, r) for s, r in rows]
