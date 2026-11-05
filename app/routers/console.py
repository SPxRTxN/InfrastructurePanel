"""SSH-доступ к серверу из браузера (право servers.console): консоль, сохранённые входы (ОС / BMC), ключи хостов,
передача файлов. Как защищена консоль — в начале app/services/console.py, ключи хостов — app/services/ssh_access.py."""
import asyncio
import json
import time
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models import Node, Server, ServerCredential, SshHostKey, User, UserSession
from app.services import agent_tunnel, audit, console, recording, ssh_access
from app.services.security import encrypt, hash_token
from app.services.settings_registry import MODULES, get_setting
from app.utils import utcnow
from app.web import SESSION_COOKIE, load_session, render, require

router = APIRouter()

FIRST_MESSAGE_TIMEOUT = 10  # с: браузер шлёт билет сразу после открытия
RECHECK = 60  # с: как часто проверять, что сессия жива (выход, отзыв сессии → консоль закрывается)


def _server(db: Session, server_id: int) -> Server:
    server = db.get(Server, server_id)
    if not server:
        raise HTTPException(404, "Сервер не найден")
    return server


def _cred(db: Session, server: Server, raw) -> ServerCredential | None:
    """Вход по id из запроса; чужой сервер или нет такого — 404. Пусто — «вручную»."""
    if raw in (None, "", "0", 0):
        return None
    try:
        cred = db.get(ServerCredential, int(raw))
    except (TypeError, ValueError):
        cred = None
    if cred is None or cred.server_id != server.id:
        raise HTTPException(404, "Вход не найден")
    return cred


def access_info(server: Server) -> dict:
    """Для страниц: входы без паролей и путей, запомненные ключи хостов с отпечатками, команда по умолчанию."""
    return {
        "command": ssh_access.ssh_command(server.ssh_user or "user", server.host, server.ssh_port),
        "creds": [{"id": c.id, "kind": c.kind, "label": c.label or c.kind.upper(),
                   "address": ssh_access.ssh_command(c.username, c.host or server.host, c.port),
                   "host": c.host or server.host, "has_password": c.has_password}
                  for c in server.credentials],
        # Отпечаток ключа не показываем (решение владельца) — только адрес и когда запомнен
        "host_keys": [{"id": k.id, "address": f"{k.host}:{k.port}", "since": k.created_at}
                      for k in server.host_keys],
    }


def _agent_node(db: Session, server: Server) -> Node | None:
    return db.scalar(select(Node).where(Node.server_id == server.id))


def _via_node(db: Session, server: Server, via) -> int:
    """via == "agent" -> id узла сервера (ошибка с понятным текстом, если узла нет или он не готов), иначе 0."""
    if via != "agent":
        return 0
    node = _agent_node(db, server)
    if node is None:
        raise ssh_access.BadTarget("к серверу не привязан агент: «через агента» недоступно")
    try:
        agent_tunnel.precheck(agent_tunnel.ref(node))
    except agent_tunnel.TunnelError as exc:
        raise ssh_access.BadTarget(str(exc)) from None
    return node.id


def _remember(server_id: int, target: ssh_access.Target, line: str, login: str, ip: str) -> None:
    """Первое подключение: ключ хоста запоминается (accept-new) и попадает в журнал с отпечатком."""
    with SessionLocal() as db:
        server = db.get(Server, server_id)
        if server is None:
            return
        ssh_access.remember_key(server, target.host, target.port, line)
        db.commit()
        audit.log_event(db, "ssh_access", username=login, ip=ip,
                        detail=f"{server.name}, {target.host}:{target.port}: ключ хоста запомнен")


# ---------- Страница консоли ----------

@router.get("/servers/{server_id}/console")
def console_page(server_id: int, request: Request, user: User = Depends(require("servers.console")),
                 db: Session = Depends(get_db)):
    server = _server(db, server_id)
    # Ключ — только факт «есть/нет»: путь к нему в UI не попадает
    node = _agent_node(db, server)
    srv = {"id": server.id, "name": server.name, "host": server.host, "port": server.ssh_port,
           "ssh_user": server.ssh_user, "has_key": server.has_key,
           "max_upload_mb": get_setting(db, "console.max_upload_mb"),
           "address": f"{server.host}:{server.ssh_port}",
           # «Через агента» предлагаем, только если узел на связи и объявил tunnel
           "agent": node.name if node and agent_tunnel.available(node) else "",
           "settings_url": _settings_url(db, server.id), "max_tabs": console.MAX_PER_USER}
    return render(request, "servers/console.html",
                  {"title": f"Консоль — {server.name}", "srv": srv, "access": access_info(server),
                   "rec_mode": get_setting(db, "console.record")}, user, db)


@router.post("/servers/{server_id}/console/ticket")
def console_ticket(server_id: int, request: Request, user: User = Depends(require("servers.console")),
                   db: Session = Depends(get_db)):
    _server(db, server_id)
    row = load_session(request, db)
    return JSONResponse({"ticket": console.issue(user.id, row.token_hash, server_id)})


# ---------- Сохранённые входы и ключи хостов ----------

def _settings_url(db: Session, server_id: int) -> str:
    """Блок входов в настройках узла. Сразу на страницу узла: через промежуточный редирект
    /servers/{id}/settings браузер терял якорь #access."""
    node = db.scalar(select(Node).where(Node.server_id == server_id))
    return f"/nodes/{node.id}#access" if node else f"/servers/{server_id}/settings#access"


def _back(db: Session, server_id: int) -> RedirectResponse:
    return RedirectResponse(_settings_url(db, server_id), 303)


@router.post("/servers/{server_id}/credentials")
async def credential_create(server_id: int, request: Request, user: User = Depends(require("servers.console")),
                            db: Session = Depends(get_db)):
    server = _server(db, server_id)
    form = await request.form()
    kind = str(form.get("kind", "os"))
    username = str(form.get("username", "")).strip()
    host = str(form.get("host", "")).strip()
    try:
        port = int(form.get("port") or ssh_access.DEFAULT_PORT)  # как у ssh: не указан — 22
    except ValueError:
        port = 0
    if kind not in ("os", "bmc") or not username or len(username) > 64 or len(host) > 255 or not 1 <= port <= 65535:
        raise HTTPException(400, "Проверьте тип, пользователя, адрес и порт")
    password = str(form.get("password", ""))
    if len(password) > 256:
        raise HTTPException(400, "Слишком длинный пароль")
    cred = ServerCredential(server_id=server.id, kind=kind, label=str(form.get("label", "")).strip()[:64],
                            host="" if host == server.host else host, port=port, username=username,
                            password_enc=encrypt(password) if password else "")
    db.add(cred)
    db.commit()
    audit.log_event(db, "ssh_access", request=request, username=user.username,
                    detail=f"{server.name}: добавлен вход {kind} {username}@{host or server.host}:{port}"
                           f"{' с паролем' if password else ''}")
    return _back(db, server.id)


@router.post("/credentials/{cred_id}/delete")
def credential_delete(cred_id: int, request: Request, user: User = Depends(require("servers.console")),
                      db: Session = Depends(get_db)):
    cred = db.get(ServerCredential, cred_id)
    if cred is None:
        raise HTTPException(404, "Вход не найден")
    server_id, detail = cred.server_id, f"{cred.server.name}: удалён вход {cred.kind} {cred.username}"
    db.delete(cred)
    db.commit()
    audit.log_event(db, "ssh_access", request=request, username=user.username, detail=detail)
    return _back(db, server_id)


@router.post("/hostkeys/{key_id}/reset")
def hostkey_reset(key_id: int, request: Request, user: User = Depends(require("servers.console")),
                  db: Session = Depends(get_db)):
    """Забыть ключ адреса+порта: следующее подключение запомнит новый (после переустановки ОС или BMC)."""
    key = db.get(SshHostKey, key_id)
    if key is None:
        raise HTTPException(404, "Ключ не найден")
    server_id = key.server_id
    detail = f"{key.server.name}, {key.host}:{key.port}: сброшен ключ хоста"
    db.delete(key)
    db.commit()
    audit.log_event(db, "ssh_access", request=request, username=user.username, detail=detail)
    return _back(db, server_id)


# ---------- Передача файлов (SFTP) ----------

def remote_path(name: str) -> str:
    """Файл всегда кладётся в ~/ пользователя, под которым выполнен вход: у SFTP домашний каталог — текущий,
    поэтому путь — просто имя. Без «/» и «..» выйти из домашнего каталога нельзя."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name or len(name) > 255:
        raise HTTPException(400, "Недопустимое имя файла")
    return name


class _TooLarge(Exception):
    pass


@router.put("/servers/{server_id}/files")
async def file_upload(server_id: int, request: Request, user: User = Depends(require("servers.console")),
                      db: Session = Depends(get_db)):
    """Тело запроса — сам файл (не multipart): поток идёт сразу в SFTP, без временного файла в памяти панели."""
    import asyncssh

    q = request.query_params
    server = _server(db, server_id)
    cred = _cred(db, server, q.get("cred"))
    path = remote_path(q.get("name", ""))
    limit = get_setting(db, "console.max_upload_mb") * 1024 * 1024
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > limit:
        raise HTTPException(413, f"Файл больше {limit // 1024 // 1024} МБ (Настройки → Безопасность)")
    # Вход вручную: команда ssh и пароль в заголовках, в процентной кодировке (кириллица); пароль — никуда в журналы
    try:
        target = ssh_access.resolve(server, cred, unquote(request.headers.get("x-ssh-command", ""))[:300],
                                    unquote(request.headers.get("x-ssh-password", ""))[:256],
                                    _via_node(db, server, request.headers.get("x-ssh-via")))
    except ssh_access.BadTarget as exc:
        raise HTTPException(400, str(exc)) from None
    ip = request.client.host if request.client else ""
    where = f"{server.name}, {target.label} {target.address}: ~/{path}"
    try:
        conn, new_key = await ssh_access.connect(target)
    except Exception as exc:  # причины asyncssh разнообразны; наружу — только понятный текст
        audit.log_event(db, "file_upload", ok=False, request=request, username=user.username,
                        detail=f"{where}: {ssh_access.error_text(exc)}")
        raise HTTPException(502, ssh_access.error_text(exc)) from None
    if new_key:
        _remember(server.id, target, new_key, user.username, ip)
    size = 0
    try:
        try:
            async with conn:
                async with conn.start_sftp_client() as sftp:
                    if q.get("overwrite") != "1" and await sftp.exists(path):
                        raise HTTPException(409, "Такой файл уже есть — включите «заменить»")
                    try:
                        async with sftp.open(path, "wb") as f:
                            async for chunk in request.stream():
                                size += len(chunk)
                                if size > limit:
                                    raise _TooLarge
                                await f.write(chunk)
                    except _TooLarge:
                        await sftp.remove(path)  # не оставляем обрезанный файл
                        raise HTTPException(413, f"Файл больше {limit // 1024 // 1024} МБ") from None
        except asyncssh.SFTPNoSuchFile:
            raise HTTPException(400, "Нет домашнего каталога у этого пользователя") from None
        except asyncssh.SFTPPermissionDenied:
            raise HTTPException(403, "Нет прав на запись в домашний каталог") from None
        except asyncssh.SFTPError:
            raise HTTPException(502, "Сервер отказал в записи файла") from None
    except HTTPException as exc:  # отказы тоже в журнал: кто и куда пытался записать
        audit.log_event(db, "file_upload", ok=False, request=request, username=user.username,
                        detail=f"{where}: {exc.detail}")
        raise
    audit.log_event(db, "file_upload", request=request, username=user.username, detail=f"{where}, {size} Б")
    return JSONResponse({"ok": True, "path": f"~/{path}", "user": target.username, "size": size})


# ---------- WebSocket консоли ----------

def _same_origin(ws: WebSocket) -> bool:
    origin, host = ws.headers.get("origin", ""), ws.headers.get("host", "")
    return bool(origin and host) and urlparse(origin).netloc == host


def _user_for(db: Session, ws: WebSocket, ticket: console.Ticket, touch: bool = False) -> User | None:
    """Сессия из cookie та же, что получила билет, жива, и у пользователя всё ещё есть право."""
    token = ws.cookies.get(SESSION_COOKIE)
    if not token or hash_token(token) != ticket.session_hash:
        return None
    row = db.scalar(select(UserSession).where(UserSession.token_hash == ticket.session_hash))
    if (row is None or row.user_id != ticket.user_id or row.expires_at < utcnow() or not row.user.is_active
            or row.user.must_change_credentials):
        return None
    enabled = set(get_setting(db, "modules.enabled"))
    row.user.disabled_modules = frozenset(m for m, _ in MODULES if m not in enabled)
    if not row.user.can("servers.console"):
        return None
    if touch:  # работа в консоли — активность: веб-сессия не истекает по бездействию
        row.last_seen = utcnow()
        db.commit()
    return row.user


async def _send(ws: WebSocket, kind: str, text: str = "", **extra) -> None:
    try:
        await ws.send_text(json.dumps({"t": kind, "m": text, **extra}, ensure_ascii=False))
    except (WebSocketDisconnect, RuntimeError):
        pass


async def _close(ws: WebSocket, code: int = 1000) -> None:
    try:
        await ws.close(code)
    except RuntimeError:
        pass


@router.websocket("/servers/{server_id}/console/ws")
async def console_ws(ws: WebSocket, server_id: int):
    if not _same_origin(ws):  # чужая страница с cookie пользователя — отказ до accept
        await _close(ws, 1008)
        return
    await ws.accept()
    ip = ws.client.host if ws.client else ""
    try:
        first = console.parse(await asyncio.wait_for(ws.receive_text(), FIRST_MESSAGE_TIMEOUT)) or {}
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        await _close(ws, 1008)
        return
    ticket = console.redeem(str(first.get("ticket", "")), server_id)
    with SessionLocal() as db:
        user = _user_for(db, ws, ticket) if ticket else None
        server = db.get(Server, server_id)
        if user is None or server is None:
            await _send(ws, "e", "нет доступа — обновите страницу")
            await _close(ws, 1008)
            return
        try:
            cred = _cred(db, server, first.get("cred"))
        except HTTPException:
            await _send(ws, "e", "сохранённый вход не найден — обновите страницу")
            await _close(ws, 1008)
            return
        try:
            target = ssh_access.resolve(server, cred, str(first.get("cmd") or "")[:300],
                                        str(first.get("password") or "")[:256],
                                        _via_node(db, server, first.get("via")))
        except ssh_access.BadTarget as exc:
            await _send(ws, "e", str(exc))
            await _close(ws, 1008)
            return
        size = console.clamp_size(first.get("c"), first.get("r"))
        login, user_id, server_name = user.username, user.id, server.name
        where = f"{server_name}, {target.label} {target.address}"
        idle_minutes = get_setting(db, "console.idle_minutes")
        rec = (recording.Recorder(*size, get_setting(db, "console.record_max_mb") * 1024 * 1024)
               if recording.wanted(db, first.get("record")) else None)
        if not console.acquire(user_id):
            await _send(ws, "e", f"открыто максимум консолей ({console.MAX_PER_USER}) — закройте лишние")
            await _close(ws, 1008)
            return
    first = None  # пароль больше не держим
    started, reason = time.monotonic(), "закрыта"
    conn = None
    try:
        try:
            conn, process, new_key = await console.open_shell(target, *size)
        except Exception as exc:  # причины asyncssh разнообразны; наружу — только понятный текст
            with SessionLocal() as db:
                audit.log_event(db, "console_open", ok=False, username=login, ip=ip,
                                detail=f"{where}: {console.error_text(exc)}")
            await _send(ws, "e", f"{target.address}: {console.error_text(exc)}",
                        **({"code": "auth"} if console.is_auth_error(exc) else {}))
            return
        target.password = ""
        if new_key:
            _remember(server_id, target, new_key, login, ip)
        with SessionLocal() as db:
            audit.log_event(db, "console_open", username=login, ip=ip, detail=where)
        await _send(ws, "o")
        reason = await _relay(ws, process, ticket, idle_minutes * 60, rec)
    finally:
        if conn is not None:
            conn.close()
        console.release(user_id)
        with SessionLocal() as db:
            note = ""
            if rec is not None and conn is not None:  # запись есть, только если оболочка открылась
                try:
                    note = f", запись #{rec.save(db, server_id=server_id, server_name=server_name, user_id=user_id, username=login, target=where, reason=reason)}"
                except Exception:  # сбой записи не должен мешать закрытию и журналу
                    note = ", запись не сохранена"
            audit.log_event(db, "console_close", username=login, ip=ip,
                            detail=f"{where}: {round(time.monotonic() - started)} с, {reason}{note}")
        await _close(ws)


async def _relay(ws: WebSocket, process, ticket: console.Ticket, idle_limit: int, rec=None) -> str:
    """Перекачка до закрытия любой стороны. Возвращает причину для журнала."""

    async def ssh_to_browser():
        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
                return "сеанс на сервере завершён"
            if rec is not None:
                rec.write(chunk)
            await ws.send_bytes(chunk)

    async def browser_to_ssh():
        last_input = last_check = time.monotonic()
        typed = False
        while True:
            now = time.monotonic()
            if now - last_input > idle_limit:
                await _send(ws, "e", "закрыто после бездействия")
                return "бездействие"
            if now - last_check > RECHECK:
                with SessionLocal() as db:
                    if _user_for(db, ws, ticket, touch=typed) is None:
                        await _send(ws, "e", "сессия завершена или право отозвано")
                        return "сессия завершена"
                last_check, typed = now, False
            wait = max(1.0, min(idle_limit - (now - last_input), RECHECK - (now - last_check)))
            try:
                raw = await asyncio.wait_for(ws.receive_text(), wait)
            except asyncio.TimeoutError:
                continue
            msg = console.parse(raw)
            if msg is None:
                continue
            if msg.get("t") == "i" and isinstance(msg.get("d"), str):
                process.stdin.write(msg["d"].encode())
                last_input, typed = time.monotonic(), True
            elif msg.get("t") == "r":
                cols, rows = console.clamp_size(msg.get("c"), msg.get("r"))
                process.change_terminal_size(cols, rows)
                if rec is not None:
                    rec.resize(cols, rows)

    tasks = [asyncio.create_task(ssh_to_browser()), asyncio.create_task(browser_to_ssh())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        task = done.pop()
        try:
            return task.result()
        except (WebSocketDisconnect, RuntimeError):
            return "браузер закрыл консоль"
        except Exception as exc:
            return console.error_text(exc)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
