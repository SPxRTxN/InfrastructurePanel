"""«Инфраструктура → Оповещения»: что горит, правила, тишина, каналы доставки, журнал.

Права: смотреть — `servers.view`; правила и тишина — `servers.edit`; каналы (токен бота, webhook) — `settings.manage`.
"""
import asyncio
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AlertLog, AlertRule, Group, User
from app.routers.home import kpi
from app.services import alerts, audit
from app.services.security import encrypt
from app.services.settings_registry import get_setting, set_setting
from app.utils import utcnow
from app.web import render, require

router = APIRouter()


def _rule(db: Session, rule_id: int) -> AlertRule:
    rule = db.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(404, "Правило не найдено")
    return rule


def _page(request: Request, db: Session, user: User, flash: str = "", status_code: int = 200):
    ch = alerts.channels(db)
    return render(request, "alerts.html", {
        "title": "Оповещения", "firing": alerts.firing(db), "kinds": alerts.KINDS, "ops": list(alerts.OPS),
        "rules": db.scalars(select(AlertRule).order_by(AlertRule.id)).all(),
        "log": db.scalars(select(AlertLog).order_by(AlertLog.id.desc()).limit(100)).all(),
        "silence": alerts.silenced(db),
        "all_groups": db.scalars(select(Group).order_by(Group.name)).all(),
        # Токен не показываем — только «задан/нет»
        "ch": {"has_token": bool(ch["telegram_token"]), "chat": ch["telegram_chat"], "webhook_url": ch["webhook_url"],
               "webhook_kind": ch["webhook_kind"]},
        "flash": flash or request.query_params.get("flash", "")[:200],
    }, user, db, status_code=status_code)


@router.get("/alerts")
def alerts_page(request: Request, user: User = Depends(require("servers.view")), db: Session = Depends(get_db)):
    return _page(request, db, user)


def _apply(db: Session, rule: AlertRule, form) -> None:
    kind = str(form.get("kind", rule.kind or ""))
    name = str(form.get("name", "")).strip()[:128] or alerts.KINDS.get(kind, "")
    if kind not in alerts.KINDS:
        raise HTTPException(400, "Неизвестный вид правила")
    raw = str(form.get("threshold", "")).strip().replace(",", ".")
    try:
        threshold = float(raw) if raw else None
        for_minutes = int(form.get("for_minutes") or 0)
        repeat = int(form.get("repeat_minutes") or 0)
    except ValueError:
        raise HTTPException(400, "Порог и минуты — числа") from None
    if kind in alerts.NEEDS_THRESHOLD and threshold is None:
        raise HTTPException(400, "Для этого вида нужен порог")
    if not (0 <= for_minutes <= 1440 and 0 <= repeat <= 10080):
        raise HTTPException(400, "Минуты: держится 0–1440, повтор 0–10080")
    sensor = str(form.get("sensor", "")).strip()[:200]
    if kind == "sensor" and not sensor:
        raise HTTPException(400, "Укажите id датчика, например systemd.nginx.service.active")
    op = str(form.get("op", ">="))
    if op not in alerts.OPS:
        raise HTTPException(400, "Неверное сравнение")
    raw_ids = form.getlist("group_ids")
    if not all(str(i).isdigit() for i in raw_ids):
        raise HTTPException(400, "Неверная группа получателей")
    ids = {int(i) for i in raw_ids}
    groups = list(db.scalars(select(Group).where(Group.id.in_(ids)))) if ids else []
    if len(groups) != len(ids):
        raise HTTPException(400, "Группа получателей не найдена")
    rule.groups = groups
    rule.name, rule.kind, rule.threshold, rule.sensor, rule.op = name, kind, threshold, sensor, op
    rule.target = str(form.get("target", "")).strip()[:128]
    rule.for_minutes, rule.repeat_minutes = for_minutes, repeat
    rule.severity = "critical" if form.get("severity") == "critical" else "warning"
    rule.enabled = form.get("enabled") == "on"
    rule.notify_bell = form.get("notify_bell") == "on"
    rule.notify_telegram = form.get("notify_telegram") == "on"
    rule.notify_webhook = form.get("notify_webhook") == "on"


@router.post("/alerts/rules")
async def rule_create(request: Request, user: User = Depends(require("servers.edit")), db: Session = Depends(get_db)):
    rule = AlertRule()
    _apply(db, rule, await request.form())
    db.add(rule)
    db.commit()
    audit.log_event(db, "alert_config", request=request, username=user.username, detail=f"правило «{rule.name}» создано")
    return RedirectResponse("/alerts#rules", 303)


@router.post("/alerts/rules/{rule_id}")
async def rule_update(rule_id: int, request: Request, user: User = Depends(require("servers.edit")),
                      db: Session = Depends(get_db)):
    rule = _rule(db, rule_id)
    _apply(db, rule, await request.form())
    db.commit()
    audit.log_event(db, "alert_config", request=request, username=user.username, detail=f"правило «{rule.name}» изменено")
    return RedirectResponse("/alerts#rules", 303)


@router.post("/alerts/rules/{rule_id}/delete")
def rule_delete(rule_id: int, request: Request, user: User = Depends(require("servers.edit")),
                db: Session = Depends(get_db)):
    rule = _rule(db, rule_id)
    name = rule.name
    db.delete(rule)
    db.commit()
    audit.log_event(db, "alert_config", request=request, username=user.username, detail=f"правило «{name}» удалено")
    return RedirectResponse("/alerts#rules", 303)


@router.post("/alerts/defaults")
def rules_defaults(request: Request, user: User = Depends(require("servers.edit")), db: Session = Depends(get_db)):
    if db.scalar(select(func.count()).select_from(AlertRule)):
        raise HTTPException(400, "Правила уже есть")
    db.add_all(alerts.default_rules())
    db.commit()
    audit.log_event(db, "alert_config", request=request, username=user.username, detail="добавлены стандартные правила")
    return RedirectResponse("/alerts#rules", 303)


@router.post("/alerts/silence")
async def silence(request: Request, user: User = Depends(require("servers.edit")), db: Session = Depends(get_db)):
    raw = str((await request.form()).get("minutes", "0"))
    minutes = int(raw) if raw.isdigit() else 0
    if minutes > 7 * 24 * 60:
        raise HTTPException(400, "Не дольше недели")
    until = (utcnow() + timedelta(minutes=minutes)).isoformat(timespec="seconds") if minutes else ""
    set_setting(db, "alerts.silence_until", until)
    audit.log_event(db, "alert_config", request=request, username=user.username,
                    detail=f"тишина на {minutes} мин" if minutes else "тишина снята")
    return RedirectResponse("/alerts", 303)


@router.post("/alerts/channels")
async def channels_save(request: Request, user: User = Depends(require("settings.manage")),
                        db: Session = Depends(get_db)):
    form = await request.form()
    token = str(form.get("telegram_token", "")).strip()
    if token:
        set_setting(db, "alerts.telegram_token", encrypt(token[:200]))
    if form.get("clear_token") == "on":
        set_setting(db, "alerts.telegram_token", "")
    set_setting(db, "alerts.telegram_chat", str(form.get("telegram_chat", "")).strip()[:64])
    url = str(form.get("webhook_url", "")).strip()[:500]
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Webhook: адрес http:// или https://")
    set_setting(db, "alerts.webhook_url", url)
    set_setting(db, "alerts.webhook_kind", "json" if form.get("webhook_kind") == "json" else "ntfy")
    audit.log_event(db, "alert_config", request=request, username=user.username,
                    detail="каналы оповещений изменены" + (" (новый токен бота)" if token else ""))
    return RedirectResponse("/alerts#channels", 303)


@router.post("/alerts/test")
async def channels_test(request: Request, user: User = Depends(require("settings.manage")),
                        db: Session = Depends(get_db)):
    ch = alerts.channels(db)
    title, body = "Проверка оповещений Homelab", f"Отправил {user.username}. Если вы это читаете — канал работает."
    results = ["колокольчик: ok"]
    alerts.bell(db, title, body, "/alerts")
    db.commit()
    if ch["telegram_token"] and ch["telegram_chat"]:
        err = await asyncio.to_thread(alerts.send_telegram, ch["telegram_token"], ch["telegram_chat"], f"{title}\n{body}")
        results.append("telegram: " + (err or "ok"))
    if ch["webhook_url"]:
        err = await asyncio.to_thread(alerts.send_webhook, ch["webhook_url"], ch["webhook_kind"], title, body,
                                      "warning", "firing")
        results.append("webhook: " + (err or "ok"))
    return _page(request, db, user, "Проверка: " + "; ".join(results))


@kpi("servers.view")
def _alerts_kpi(db: Session, user: User) -> dict:
    count = len(alerts.firing(db))
    return {"icon": "bell", "label": "Тревог сейчас", "value": str(count), "link": "/alerts"}
