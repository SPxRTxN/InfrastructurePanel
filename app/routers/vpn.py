"""«Инфраструктура → VPN»: подписки и сервер для узла. Только `settings.manage`: маршрутизация узла — решение
администратора, а в подписке — личные ключи.

Узел получает конфиг (сообщение `vpn` протокола агента), только если сам объявил `features: ["vpn"]` в hello,
то есть владелец разрешил это в конфиге агента. Агент применяет, проверяет связь и при неудаче откатывается.
"""
import asyncio
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Node, NodeEvent, User, VpnAssignment, VpnSubscription
from app.services import agents, audit, vpn
from app.services.security import decrypt, encrypt
from app.utils import utcnow
from app.web import render, require

router = APIRouter()


def _sub(db: Session, sub_id: int) -> VpnSubscription:
    sub = db.get(VpnSubscription, sub_id)
    if not sub:
        raise HTTPException(404, "Подписка не найдена")
    return sub


_BACK = re.compile(r"^/(servers/\d+/settings|nodes/\d+)(#vpn)?$")


def _back(form, default: str) -> str:
    """Куда вернуться после назначения: только свои страницы настроек узла, чужие адреса игнорируем."""
    back = str(form.get("back", ""))
    return back if _BACK.fullmatch(back) else default  # fullmatch: «$» пропустил бы \n в конце


def supports_vpn(node: Node) -> bool:
    return "vpn" in node.info.get("features", [])


async def refresh(db: Session, sub: VpnSubscription) -> None:
    try:
        body = await asyncio.to_thread(vpn.fetch, decrypt(sub.url_enc), sub.insecure)
        servers, errors = vpn.parse(body)
        if not servers:
            raise vpn.SubscriptionError("в подписке нет поддерживаемых серверов" + (f" ({errors[0]})" if errors else ""))
        sub.servers_enc, sub.count, sub.fetched_at = vpn.pack(servers), len(servers), utcnow()
        sub.error = f"пропущено {len(errors)}: {errors[0]}" if errors else ""
    except vpn.SubscriptionError as e:
        sub.error = str(e)[:500]
    db.commit()


def message_for(db: Session, a: VpnAssignment) -> dict:
    """Сообщение узлу: конфиг клиента или выключение. Адрес панели агент добавит в обход туннеля сам."""
    if a.mode == "off":
        return {"type": "vpn", "id": f"v-{a.id}", "mode": "off"}
    server = next((s for s in vpn.unpack(a.subscription.servers_enc) if s["key"] == a.server_key), None)
    if not server:
        raise vpn.SubscriptionError("сервера больше нет в подписке — обновите её и выберите снова")
    if a.fingerprint:
        server = {**server, "fingerprint": a.fingerprint}
    config, xray = vpn.engine_configs(server, a.fingerprint)
    return {"type": "vpn", "id": f"v-{a.id}", "mode": a.mode, "server": vpn.public(server),
            "config": config, "xray": xray}


# В журнал узла (виден с servers.view) — без того, что похоже на ключи: uuid и длинные base64-строки маскируются
_SECRETISH = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[A-Za-z0-9+/_=-]{32,}")


def vpn_event(db: Session, node_id: int, text: str, level: str = "info") -> None:
    """Строка журнала подключения VPN (плитки «События» и «VPN»)."""
    db.add(NodeEvent(node_id=node_id, ts=utcnow(), level=level, text=("VPN: " + _SECRETISH.sub("•••", text))[:500]))


async def push(db: Session, a: VpnAssignment) -> None:
    """Отправить узлу, если он на связи и умеет; иначе — ждать (отправится при следующем hello)."""
    node = a.node
    try:
        message = message_for(db, a)
    except vpn.SubscriptionError as e:  # вызывается и из hello агента — исключение оборвало бы его соединение
        a.status, a.detail = "failed", str(e)[:500]
    else:
        if not supports_vpn(node):
            a.status, a.detail = "unsupported", "агент узла не объявил поддержку VPN (нужно [vpn] enabled в конфиге агента)"
        elif not await agents.hub.send(node.id, message):
            a.status, a.detail = "pending", "узел не на связи — отправится при подключении"
        else:
            a.status, a.detail = "sent", "отправлено, ждём подтверждения узла"
    if a.mode == "off":
        what = "выключение"
    else:
        what = f"сервер «{a.server_name}»" + (f", отпечаток {a.fingerprint}" if a.fingerprint else "")
    vpn_event(db, node.id, f"{what} — {a.detail}", "warning" if a.status in ("failed", "unsupported") else "info")
    a.updated_at = utcnow()
    db.commit()


async def on_hello(db: Session, node: Node) -> None:
    """Подключился узел — дослать назначение, если оно ещё не применено."""
    a = db.scalar(select(VpnAssignment).where(VpnAssignment.node_id == node.id))
    if a and a.status in ("pending", "sent", "unsupported"):
        await push(db, a)


def on_result(db: Session, node: Node, data: dict) -> None:
    a = db.scalar(select(VpnAssignment).where(VpnAssignment.node_id == node.id))
    if not a or data.get("id") != f"v-{a.id}":
        return
    a.status = "applied" if data.get("ok") is True else "failed"
    a.detail = str(data.get("detail", ""))[:500]
    engine = data.get("engine")
    a.engine = engine if engine in ("sing-box", "xray") else ""
    a.updated_at = utcnow()
    if a.mode == "off":
        text = "выключен" if a.status == "applied" else f"не выключился: {a.detail}"
    elif a.status == "applied":
        text = f"включён: «{a.server_name}» через {a.engine or 'туннель'}" + (f" ({a.detail})" if a.detail else "")
    else:
        text = f"откатился: {a.detail}"
    vpn_event(db, node.id, text, "info" if a.status == "applied" else "warning")
    if "interfaces" in data:  # свежий список после смены: TUN виден сразу, не ждём переподключения агента
        info = node.info
        info["interfaces"] = agents.parse_interfaces(data["interfaces"])
        node.info_json = json.dumps(info, ensure_ascii=False)
    db.commit()


# ---------- Страница ----------

@router.get("/vpn")
def vpn_page(request: Request, user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    subs = db.scalars(select(VpnSubscription).order_by(VpnSubscription.name)).all()
    servers = {s.id: [vpn.public(x) for x in vpn.unpack(s.servers_enc)] for s in subs}
    return render(request, "vpn.html", {
        "title": "VPN", "subs": subs, "servers": servers,
        "nodes": db.scalars(select(Node).order_by(Node.name)).all(),
        "assignments": {a.node_id: a for a in db.scalars(select(VpnAssignment))},
        "supports": supports_vpn,
    }, user, db)


@router.get("/servers/{server_id}/vpn")
def server_vpn(server_id: int, request: Request, user: User = Depends(require("settings.manage")),
               db: Session = Depends(get_db)):
    """Блок VPN в настройках узла (подгружается htmx): нет узла / VPN не разрешён / выбор сервера."""
    node = db.scalar(select(Node).where(Node.server_id == server_id))
    subs = db.scalars(select(VpnSubscription).order_by(VpnSubscription.name)).all()
    return render(request, "servers/_vpn.html", {
        "srv_id": server_id, "node": node, "subs": subs,
        "servers": {s.id: [vpn.public(x) for x in vpn.unpack(s.servers_enc)] for s in subs},
        "assignment": db.scalar(select(VpnAssignment).where(VpnAssignment.node_id == node.id)) if node else None,
        "supported": bool(node) and supports_vpn(node),
        "singbox": (node.info.get("vpn") or {}).get("singbox", "") if node else "",
        "fingerprints": vpn.FINGERPRINTS,
        "vpn_log": db.scalars(select(NodeEvent).where(NodeEvent.node_id == node.id, NodeEvent.text.startswith("VPN: "))
                              .order_by(NodeEvent.ts.desc(), NodeEvent.id.desc()).limit(8)).all() if node else [],
    }, user, db)


@router.post("/vpn/subscriptions")
async def sub_create(request: Request, user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    form = await request.form()
    url = str(form.get("url", "")).strip()
    if not vpn.valid_url(url):
        raise HTTPException(400, "Ссылка подписки: https://… (или http://)")
    sub = VpnSubscription(name=str(form.get("name", "")).strip()[:128] or "Подписка", url_enc=encrypt(url),
                          insecure=form.get("insecure") == "on")
    db.add(sub)
    db.commit()
    await refresh(db, sub)
    audit.log_event(db, "vpn_config", request=request, username=user.username,
                    detail=f"подписка «{sub.name}» добавлена, серверов {sub.count}")
    return RedirectResponse("/vpn", 303)


@router.post("/vpn/subscriptions/{sub_id}/refresh")
async def sub_refresh(sub_id: int, request: Request, user: User = Depends(require("settings.manage")),
                      db: Session = Depends(get_db)):
    sub = _sub(db, sub_id)
    await refresh(db, sub)
    return RedirectResponse("/vpn", 303)


@router.post("/vpn/subscriptions/{sub_id}/delete")
def sub_delete(sub_id: int, request: Request, user: User = Depends(require("settings.manage")),
               db: Session = Depends(get_db)):
    sub = _sub(db, sub_id)
    if db.scalar(select(VpnAssignment).where(VpnAssignment.subscription_id == sub.id, VpnAssignment.mode != "off")):
        raise HTTPException(409, "Подписка используется узлом — сначала выключите VPN на нём")
    name = sub.name
    db.delete(sub)
    db.commit()
    audit.log_event(db, "vpn_config", request=request, username=user.username, detail=f"подписка «{name}» удалена")
    return RedirectResponse("/vpn", 303)


@router.post("/vpn/assign")
async def assign(request: Request, user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    form = await request.form()
    node = db.get(Node, int(form.get("node_id", 0) or 0)) if str(form.get("node_id", "")).isdigit() else None
    if not node:
        raise HTTPException(400, "Выберите узел")
    sub_raw, _, key = str(form.get("server", "")).partition(":")
    sub = _sub(db, int(sub_raw)) if sub_raw.isdigit() else None
    server = next((s for s in vpn.unpack(sub.servers_enc) if s["key"] == key), None) if sub else None
    if not server:
        raise HTTPException(400, "Выберите сервер из подписки")
    fingerprint = str(form.get("fingerprint", ""))
    if fingerprint and fingerprint not in vpn.FINGERPRINTS:
        raise HTTPException(400, "Неизвестный отпечаток uTLS")
    try:
        vpn.engine_configs(server)  # не умеют ни sing-box, ни Xray — понятная ошибка сразу, а не на узле
    except vpn.SubscriptionError as e:
        raise HTTPException(400, f"Сервер «{server['name']}»: {e}. Выберите другой сервер") from None
    a = db.scalar(select(VpnAssignment).where(VpnAssignment.node_id == node.id)) or VpnAssignment(node_id=node.id)
    a.subscription_id, a.server_key, a.server_name, a.mode = sub.id, key, server["name"], "all"
    a.fingerprint = fingerprint
    db.add(a)
    db.commit()
    await push(db, a)
    audit.log_event(db, "vpn_config", request=request, username=user.username,
                    detail=f"узел «{node.name}»: весь трафик через «{server['name']}»"
                    + (f", отпечаток {fingerprint}" if fingerprint else ""))
    return RedirectResponse(_back(form, "/vpn#nodes"), 303)


@router.post("/vpn/assign/{node_id}/off")
async def assign_off(node_id: int, request: Request, user: User = Depends(require("settings.manage")),
                     db: Session = Depends(get_db)):
    form = await request.form()
    a = db.scalar(select(VpnAssignment).where(VpnAssignment.node_id == node_id))
    if not a:
        raise HTTPException(404, "VPN на узле не назначен")
    a.mode = "off"
    db.commit()
    await push(db, a)
    audit.log_event(db, "vpn_config", request=request, username=user.username, detail=f"узел «{a.node.name}»: VPN выключен")
    return RedirectResponse(_back(form, "/vpn#nodes"), 303)
