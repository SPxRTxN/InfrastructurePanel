"""«Настройки → Перенос»: экспорт и импорт конфигурации панели (права: `settings.manage`).

Импорт в два шага без хранения на сервере: предпросмотр возвращает зашифрованный пакет в скрытом поле формы,
подтверждение принимает его обратно вместе с паролем (пароль пакета в страницах не отправляется никогда).
"""
import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile  # request.form() отдаёт именно этот класс, не fastapi.UploadFile

from app.db import get_db
from app.models import User
from app.routers.settings import _page
from app.services import audit, transfer
from app.web import render, require

router = APIRouter(prefix="/settings/transfer")


def _view(request: Request, db: Session, user: User, status_code: int = 200, **ctx):
    resp = _page(request, db, user, "settings/transfer.html", "transfer", status_code=status_code,
                 sections=transfer.SECTIONS, max_kb=transfer.MAX_FILE // 1024, min_password=transfer.MIN_PASSWORD,
                 **ctx)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("")
def page(request: Request, user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    return _view(request, db, user)


@router.post("/export")
async def export(request: Request, user: User = Depends(require("settings.manage")), db: Session = Depends(get_db)):
    form = await request.form()
    password, again = str(form.get("password", "")), str(form.get("password2", ""))
    chosen = {s for s in form.getlist("sections") if s in transfer.SECTIONS}
    passwords, same_tokens = form.get("passwords") == "on", form.get("same_tokens") == "on"
    try:
        if password != again:
            raise transfer.TransferError("Пароли пакета не совпадают")
        package = transfer.build_package(db, chosen, passwords=passwords, same_tokens=same_tokens)
        blob = transfer.seal(package, password)
    except transfer.TransferError as exc:
        return _view(request, db, user, status_code=400, error=str(exc))
    audit.log_event(db, "config_export", request=request, username=user.username,
                    detail=", ".join(transfer.SECTIONS[s] for s in package["sections"])
                    + (" · с хешами паролей" if package["meta"]["passwords"] else "")
                    + (" · с хешами токенов узлов" if package["meta"]["same_tokens"] else ""))
    name = transfer.file_name()
    return Response(blob, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/preview")
async def import_preview(request: Request, user: User = Depends(require("settings.manage")),
                         db: Session = Depends(get_db)):
    form = await request.form()
    upload = form.get("file")
    try:
        if not isinstance(upload, UploadFile) or not upload.filename:
            raise transfer.TransferError("Выберите файл .hlconf")
        blob = await upload.read(transfer.MAX_FILE + 1)  # не читаем сверх лимита
        package = transfer.open_package(blob, str(form.get("password", "")))
        info = transfer.preview(db, package)
    except transfer.TransferError as exc:
        return _view(request, db, user, status_code=400, error=str(exc), tab="import")
    return _view(request, db, user, info=info, blob=base64.b64encode(blob).decode(), tab="import")


@router.post("/apply")
async def import_apply(request: Request, user: User = Depends(require("settings.manage")),
                       db: Session = Depends(get_db)):
    form = await request.form()
    try:
        try:
            blob = base64.b64decode(str(form.get("blob", "")), validate=True)
        except (binascii.Error, ValueError):
            raise transfer.TransferError("Пакет повреждён, загрузите файл заново") from None
        package = transfer.open_package(blob, str(form.get("password", "")))
        strategies = {}
        for name in package["sections"]:
            choice = str(form.get(f"strategy_{name}", "skip"))
            if choice == "off":
                continue
            if choice not in ("skip", "replace"):
                raise HTTPException(400, "Неверная стратегия")
            strategies[name] = choice
        if not strategies:
            raise transfer.TransferError("Ни один раздел не выбран")
        report = transfer.apply(db, package, strategies, actor=user.username)
    except transfer.TransferError as exc:
        # Пароль мог быть введён неверно — предпросмотр показываем заново, пакет остаётся в форме
        ctx = {"error": str(exc), "tab": "import"}
        try:
            ctx.update(info=transfer.preview(db, transfer.open_package(blob, str(form.get("password", "")))),
                       blob=str(form.get("blob", "")))
        except (transfer.TransferError, NameError, UnboundLocalError):
            pass
        return _view(request, db, user, status_code=400, **ctx)
    summary = "; ".join(f"{transfer.SECTIONS[s]}: +{c['added']} ~{c['replaced']} -{c['skipped']}"
                        for s, c in report["counts"].items())
    audit.log_event(db, "config_import", request=request, username=user.username, detail=summary)
    return _view(request, db, user, report=report, tab="import")
