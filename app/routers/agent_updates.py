"""«Инфраструктура → Агенты»: версии агента, обновление узлов из панели, массовое добавление узлов.

Права: просмотр — `servers.view`; кнопки «Обновить» и добавление узлов — `servers.edit` (как остальные действия над
узлами); загрузка и удаление архивов — `settings.manage`: архив — это код, который узлы запустят у себя (после SHA-256
и самотеста), поэтому решать, какая версия вообще доступна, должен администратор, а не любой редактор.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AgentRelease, Node, NodeUpdate, User
from app.routers.nodes import summary
from app.services import agent_updates as svc
from app.services import audit
from app.web import render, require

router = APIRouter()


def _release(db: Session, release_id) -> AgentRelease:
    release = db.get(AgentRelease, int(release_id)) if str(release_id).isdigit() else None
    if not release:
        raise HTTPException(400, "Выберите версию из каталога")
    return release


# ---------- Страница ----------

@router.get("/agents")
def agents_page(request: Request, user: User = Depends(require("servers.view")), db: Session = Depends(get_db)):
    svc.expire(db)
    releases = sorted(db.scalars(select(AgentRelease)).all(), key=lambda r: svc.version_key(r.version), reverse=True)
    newest = svc.latest(releases)
    last: dict[int, NodeUpdate] = {}
    for upd in db.scalars(select(NodeUpdate).order_by(NodeUpdate.id)):
        last[upd.node_id] = upd
    nodes = []
    for node in db.scalars(select(Node).order_by(Node.name)):
        info = summary(db, node)
        info["can_update"] = svc.supports_update(node)
        info["last_update"] = last.get(node.id)
        info["outdated"] = bool(newest and node.version and svc.version_key(node.version) < svc.version_key(newest.version))
        nodes.append(info)
    log = db.scalars(select(NodeUpdate).order_by(NodeUpdate.id.desc()).limit(30)).all()
    return render(request, "agents/list.html", {
        "title": "Агенты", "releases": releases, "newest": newest, "nodes": nodes, "log": log,
        "ws_url": str(request.base_url).replace("http", "ws", 1) + "agent/ws",
    }, user, db)


# ---------- Каталог версий ----------

@router.post("/agents/releases")
async def release_upload(request: Request, user: User = Depends(require("settings.manage")),
                         db: Session = Depends(get_db)):
    form = await request.form()
    upload = form.get("archive")
    if not hasattr(upload, "read"):
        raise HTTPException(400, "Выберите файл .tar.gz")
    data = await upload.read(svc.MAX_ARCHIVE + 1)  # больше лимита не читаем в память
    try:
        release = svc.add_release(db, data, str(form.get("version", "")), str(form.get("notes", "")), user)
    except svc.ReleaseError as exc:
        raise HTTPException(400, str(exc)) from None
    audit.log_event(db, "agent_release", request=request, username=user.username,
                    detail=f"версия агента {release.version} загружена, sha256 {release.sha256[:12]}…")
    return RedirectResponse("/agents", 303)


@router.post("/agents/releases/{release_id}/delete")
def release_delete(release_id: int, request: Request, user: User = Depends(require("settings.manage")),
                   db: Session = Depends(get_db)):
    release = _release(db, release_id)
    version = release.version
    svc.delete_release(db, release)
    audit.log_event(db, "agent_release", request=request, username=user.username, detail=f"версия агента {version} удалена")
    return RedirectResponse("/agents", 303)


@router.get("/agent/releases/{filename}")
def release_download(filename: str, request: Request, db: Session = Depends(get_db)):
    """Выдача архива агенту: только по `Authorization: Bearer <токен узла>`; сессия браузера не подходит."""
    node = svc.node_by_token(db, request.headers.get("authorization"))
    if not node:
        audit.log_event(db, "agent_auth", ok=False, request=request, detail="скачивание версии агента: неверный токен")
        raise HTTPException(401, "Нужен токен узла", headers={"WWW-Authenticate": "Bearer"})
    version = filename.removesuffix(".tar.gz") if filename.endswith(".tar.gz") else ""
    release = db.scalar(select(AgentRelease).where(AgentRelease.version == version)) if version else None
    path = svc.release_path(release.version) if release else None
    if not path or not path.is_file():
        raise HTTPException(404, "Версия не найдена")
    return FileResponse(path, media_type="application/gzip", filename=f"homelab-agent-{release.version}.tar.gz")


# ---------- Обновление узлов ----------

async def _update(request: Request, user: User, db: Session, node_ids: list[int]):
    form = await request.form()
    release = _release(db, form.get("release_id"))
    nodes = [n for n in (db.get(Node, i) for i in dict.fromkeys(node_ids)) if n]
    if not nodes:
        raise HTTPException(400, "Выберите хотя бы один узел")
    for node in nodes:
        await svc.request_update(db, node, release, user)
    audit.log_event(db, "agent_update", request=request, username=user.username,
                    detail=f"обновление агентов до {release.version}: {', '.join(n.name for n in nodes)}"[:500])
    return RedirectResponse("/agents#log", 303)


@router.post("/agents/update")
async def update_selected(request: Request, user: User = Depends(require("servers.edit")), db: Session = Depends(get_db)):
    form = await request.form()
    ids = [int(v) for v in form.getlist("node_id") if str(v).isdigit()]
    return await _update(request, user, db, ids)


@router.post("/agents/update/{node_id}")
async def update_one(node_id: int, request: Request, user: User = Depends(require("servers.edit")),
                     db: Session = Depends(get_db)):
    return await _update(request, user, db, [node_id])


# ---------- Массовое добавление узлов ----------

@router.post("/agents/nodes")
async def nodes_add(request: Request, user: User = Depends(require("servers.edit")), db: Session = Depends(get_db)):
    form = await request.form()
    created, errors = svc.add_nodes(db, str(form.get("targets", ""))[:20000])
    if created:
        audit.log_event(db, "node_token", request=request, username=user.username,
                        detail=f"массово создано узлов: {', '.join(c['name'] for c in created)}"[:500])
    response = render(request, "agents/added.html", {
        "title": "Узлы добавлены", "created": created, "errors": errors,
        "ws_url": str(request.base_url).replace("http", "ws", 1) + "agent/ws",
    }, user, db)
    response.headers["Cache-Control"] = "no-store"  # на странице токены
    return response
