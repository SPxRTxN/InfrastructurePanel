"""«Инфраструктура → VPN»: подписки и сервер для узла. Только `settings.manage`: маршрутизация узла — решение
администратора, а в подписке — личные ключи.

Узел получает конфиг (сообщение `vpn` протокола агента), только если сам объявил `features: ["vpn"]` в hello,
то есть владелец разрешил это в конфиге агента. Агент применяет, проверяет связь и при неудаче откатывается.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Node, User, VpnAssignment, VpnSubscription
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
        raise HTTPException(409, "Сервера больше нет в подписке — обновите её и выберите снова")
    return {"type": "vpn", "id": f"v-{a.id}", "mode": a.mode, "server": vpn.public(server),
            "config": vpn.singbox_config(server, bypass=[])}


async def push(db: Session, a: VpnAssignment) -> None:
    """Отправить узлу, если он на связи и умеет; иначе — ждать (отправится при следующем hello)."""
    node = a.node
    if not supports_vpn(node):
        a.status, a.detail = "unsupported", "агент узла не объявил поддержку VPN (нужно [vpn] enabled в конфиге агента)"
    elif not await agents.hub.send(node.id, message_for(db, a)):
        a.status, a.detail = "pending", "узел не на связи — отправится при подключении"
    else:
        a.status, a.detail = "sent", "отправлено, ждём подтверждения узла"
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
    a.updated_at = utcnow()
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
    vpn.singbox_config(server, [])  # неподдерживаемый транспорт — понятная ошибка сразу, а не на узле
    a = db.scalar(select(VpnAssignment).where(VpnAssignment.node_id == node.id)) or VpnAssignment(node_id=node.id)
    a.subscription_id, a.server_key, a.server_name, a.mode = sub.id, key, server["name"], "all"
    db.add(a)
    db.commit()
    await push(db, a)
    audit.log_event(db, "vpn_config", request=request, username=user.username,
                    detail=f"узел «{node.name}»: весь трафик через «{server['name']}»")
    return RedirectResponse("/vpn#nodes", 303)


@router.post("/vpn/assign/{node_id}/off")
async def assign_off(node_id: int, request: Request, user: User = Depends(require("settings.manage")),
                     db: Session = Depends(get_db)):
    a = db.scalar(select(VpnAssignment).where(VpnAssignment.node_id == node_id))
    if not a:
        raise HTTPException(404, "VPN на узле не назначен")
    a.mode = "off"
    db.commit()
    await push(db, a)
    audit.log_event(db, "vpn_config", request=request, username=user.username, detail=f"узел «{a.node.name}»: VPN выключен")
    return RedirectResponse("/vpn#nodes", 303)
