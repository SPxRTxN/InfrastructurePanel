"""VPN-подписки: загрузка, разбор ссылок (vless, vmess, trojan, ss, hysteria2) и выходной конфиг для узла.

Подписка — обычно base64 со списком ссылок по строке (3x-ui, Marzban и т.п.). Ссылка подписки и параметры
серверов (uuid, пароли, ключи reality) — секреты: в БД они лежат зашифрованными (Fernet), в UI показываются только
имя, протокол, адрес и порт.

`singbox_config` строит конфиг клиента sing-box для режима «весь трафик узла через VPN»: TUN с auto_route,
мимо туннеля — частные сети и адрес панели (иначе узел потеряет связь с панелью и не сможет откатиться).
"""
import base64
import binascii
import hashlib
import ipaddress
import json
import re
import ssl
import urllib.error
import urllib.request
from urllib.parse import parse_qs, unquote, urlsplit

from app.services.security import decrypt, encrypt

TIMEOUT = 15
MAX_BODY = 2 * 1024 * 1024
MAX_SERVERS = 500
PROTOCOLS = {"vless", "vmess", "trojan", "ss", "hysteria2"}
PRIVATE_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
                "fc00::/7", "fe80::/10", "127.0.0.0/8", "::1/128"]


class SubscriptionError(ValueError):
    pass


# ---------- Загрузка ----------

def fetch(url: str, insecure: bool = False) -> str:
    if not url.startswith(("https://", "http://")):
        raise SubscriptionError("ссылка подписки должна начинаться с https://")
    ctx = ssl.create_default_context()
    if insecure:  # подписки по https://<IP> часто с самоподписанным сертификатом — решает владелец галочкой
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "homelab-dashboard (subscription)"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:  # nosec B310 — схема проверена выше
            body = r.read(MAX_BODY + 1)
    except ssl.SSLError as e:
        raise SubscriptionError(f"TLS: {e.reason or e.__class__.__name__} — если у подписки самоподписанный "
                                f"сертификат, включите «не проверять сертификат»") from None
    except urllib.error.HTTPError as e:
        raise SubscriptionError(f"сервер подписки ответил HTTP {e.code}") from None
    except (OSError, ValueError) as e:
        raise SubscriptionError(f"не удалось загрузить: {e.__class__.__name__}") from None
    if len(body) > MAX_BODY:
        raise SubscriptionError("подписка больше 2 МБ")
    return body.decode("utf-8", "replace")


# ---------- Разбор ----------

def _b64(text: str) -> str:
    text = text.strip().replace("\n", "").replace("\r", "")
    text += "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise SubscriptionError("не base64") from None


def links(body: str) -> list[str]:
    """Тело подписки → ссылки. Понимает base64-список и простой список."""
    text = body.strip()
    if "://" not in text.split("\n", 1)[0]:
        try:
            text = _b64(text)
        except SubscriptionError:
            raise SubscriptionError("формат подписки не распознан (ожидался список ссылок или base64)") from None
    return [ln.strip() for ln in text.splitlines() if "://" in ln][:MAX_SERVERS]


def _q(query: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}


def _hostport(netloc: str) -> tuple[str, int]:
    host, _, port = netloc.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise SubscriptionError("нет адреса или порта")
    return host, int(port)


def _extra(raw: str) -> dict:
    """Параметр xhttp `extra` — JSON-объект; мусор и не-объект игнорируем (ссылки чужие, падать нельзя)."""
    if not raw or len(raw) > 4096:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_link(link: str) -> dict:
    """Ссылка → нормализованный сервер {key, name, protocol, host, port, transport, security, ...секреты}."""
    scheme = link.split("://", 1)[0].lower()
    if scheme == "hy2":
        scheme = "hysteria2"
    if scheme not in PROTOCOLS:
        raise SubscriptionError(f"протокол {scheme} не поддерживается")
    key = hashlib.sha256(link.encode()).hexdigest()[:16]
    if scheme == "vmess":
        data = json.loads(_b64(link.split("://", 1)[1]))
        srv = {"protocol": "vmess", "name": str(data.get("ps", "")), "host": str(data.get("add", "")),
               "port": int(data.get("port", 0)), "uuid": str(data.get("id", "")), "alter_id": int(data.get("aid", 0) or 0),
               "transport": str(data.get("net", "tcp")), "path": str(data.get("path", "")),
               "host_header": str(data.get("host", "")), "security": "tls" if data.get("tls") == "tls" else "none",
               "sni": str(data.get("sni", "") or data.get("host", "")), "mode": str(data.get("mode", "")),
               "extra": _extra(str(data.get("extra", "")))}
        if not srv["host"] or not 0 < srv["port"] < 65536:
            raise SubscriptionError("vmess без адреса")
    else:
        u = urlsplit(link)
        q = _q(u.query)
        name = unquote(u.fragment)
        if scheme == "ss":
            userinfo, _, hostport = u.netloc.rpartition("@")
            if not userinfo:  # ss://base64(method:pass@host:port)
                decoded = _b64(u.netloc)
                userinfo, _, hostport = decoded.rpartition("@")
            else:
                userinfo = unquote(userinfo)
                if ":" not in userinfo:
                    userinfo = _b64(userinfo)
            method, _, password = userinfo.partition(":")
            host, port = _hostport(hostport)
            srv = {"protocol": "ss", "name": name, "host": host, "port": port, "method": method,
                   "password": password, "transport": "tcp", "security": "none"}
        else:
            secret = unquote(u.username or "")
            host, port = _hostport(u.netloc.rpartition("@")[2])
            srv = {"protocol": scheme, "name": name, "host": host, "port": port,
                   "transport": q.get("type", "tcp"), "security": q.get("security", "tls" if scheme != "vless" else "none"),
                   "sni": q.get("sni", "") or q.get("peer", ""), "fingerprint": q.get("fp", ""),
                   "alpn": q.get("alpn", ""), "path": q.get("path", ""), "host_header": q.get("host", ""),
                   "service_name": q.get("serviceName", ""), "mode": q.get("mode", ""), "extra": _extra(q.get("extra", "")),
                   "insecure": q.get("insecure", q.get("allowInsecure", "0")) in ("1", "true")}
            if scheme == "vless":
                srv |= {"uuid": secret, "flow": q.get("flow", ""), "public_key": q.get("pbk", ""), "short_id": q.get("sid", ""),
                             "spider_x": q.get("spx", "")}
            elif scheme == "trojan":
                srv["password"] = secret
            else:  # hysteria2
                srv |= {"password": secret, "obfs": q.get("obfs", ""), "obfs_password": q.get("obfs-password", ""),
                        "security": "tls"}
    srv["key"] = key
    srv["name"] = (srv["name"] or f"{srv['host']}:{srv['port']}")[:120]
    return srv


def parse(body: str) -> tuple[list[dict], list[str]]:
    """→ (серверы, ошибки по строкам). Ошибка одной ссылки не мешает остальным."""
    servers, errors = [], []
    for i, link in enumerate(links(body), 1):
        try:
            servers.append(parse_link(link))
        except (SubscriptionError, ValueError, KeyError, json.JSONDecodeError) as e:
            errors.append(f"строка {i}: {e}")
    return servers, errors


def public(server: dict) -> dict:
    """То, что можно показать в UI: без uuid, паролей и ключей."""
    return {k: server.get(k) for k in ("key", "name", "protocol", "host", "port", "transport", "security")}


def diagnose(server: dict) -> list[str]:
    """Форма параметров сервера без секретов (для `python -m app.cli vpn-check`): длины и допустимость ключей
    reality, SNI, отпечаток. Строки с «!» — вероятная причина «reality verification failed»."""
    lines = [f"{k}: {server.get(k) or '—'}" for k in ("protocol", "transport", "security", "sni", "fingerprint",
                                                      "flow", "alpn")]
    if server.get("transport") == "xhttp":
        lines.append(f"mode: {server.get('mode') or 'auto'}")
    sb, xr = engine_support(server)
    lines.append(f"движки: sing-box {'да' if sb else 'нет'}, xray {'да' if xr else 'нет'}")
    if server.get("security") != "reality":
        return lines
    pbk = server.get("public_key", "")
    try:
        raw = base64.urlsafe_b64decode(pbk + "=" * (-len(pbk) % 4)) if pbk else b""
    except (binascii.Error, ValueError):
        raw = b""
    lines.append(f"pbk: {len(pbk)} симв., {len(raw)} байт" + ("" if len(raw) == 32 else
                 "  ! ключ должен быть 32 байта (43 симв. base64url) — испорчен или обрезан в ссылке"))
    sid = server.get("short_id", "")
    ok_sid = len(sid) <= 16 and len(sid) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in sid)
    lines.append(f"sid: {len(sid)} симв." + ("" if ok_sid else "  ! short id — hex чётной длины до 16 символов")
                 + ("  (пустой — сервер должен разрешать пустой shortId)" if not sid else ""))
    if not server.get("sni"):
        lines.append("sni: пусто  ! SNI будет равен адресу сервера, а reality ждёт имя из serverNames")
    return lines


def pack(servers: list[dict]) -> str:
    return encrypt(json.dumps(servers, ensure_ascii=False))


def unpack(blob: str) -> list[dict]:
    return json.loads(decrypt(blob) or "[]") if blob else []


# ---------- Конфиг клиента для узла (sing-box) ----------

# Отпечатки uTLS sing-box, которые можно выбрать узлу вместо указанного в подписке
FINGERPRINTS = ("chrome", "firefox", "edge", "safari", "ios", "android", "random", "randomized")


def _tls(s: dict) -> dict | None:
    if s.get("security") not in ("tls", "reality"):
        return None
    tls = {"enabled": True, "server_name": s.get("sni") or s["host"], "insecure": bool(s.get("insecure"))}
    if s.get("alpn"):
        tls["alpn"] = [a for a in s["alpn"].split(",") if a]
    if s.get("fingerprint"):
        tls["utls"] = {"enabled": True, "fingerprint": s["fingerprint"]}
    if s.get("security") == "reality":
        tls["reality"] = {"enabled": True, "public_key": s.get("public_key", ""), "short_id": s.get("short_id", "")}
        tls.setdefault("utls", {"enabled": True, "fingerprint": "chrome"})
    return tls


def _transport(s: dict) -> dict | None:
    t = s.get("transport", "tcp")
    if t == "ws":
        out = {"type": "ws", "path": s.get("path") or "/"}
        if s.get("host_header"):
            out["headers"] = {"Host": s["host_header"]}
        return out
    if t == "grpc":
        return {"type": "grpc", "service_name": s.get("service_name") or s.get("path", "")}
    if t in ("http", "h2"):
        return {"type": "http", "path": s.get("path") or "/", "host": [s["host_header"]] if s.get("host_header") else []}
    if t in ("httpupgrade",):
        return {"type": "httpupgrade", "path": s.get("path") or "/", "host": s.get("host_header", "")}
    if t in ("tcp", "", "raw", "none"):
        return None
    raise SubscriptionError(f"транспорт {t} пока не поддерживается для sing-box")


def outbound(s: dict) -> dict:
    out: dict = {"type": {"ss": "shadowsocks"}.get(s["protocol"], s["protocol"]), "tag": "vpn",
                 "server": s["host"], "server_port": s["port"]}
    p = s["protocol"]
    if p == "vless":
        out["uuid"] = s["uuid"]
        if s.get("flow"):
            out["flow"] = s["flow"]
    elif p == "vmess":
        out |= {"uuid": s["uuid"], "alter_id": s.get("alter_id", 0), "security": "auto"}
    elif p == "trojan":
        out["password"] = s["password"]
    elif p == "ss":
        out |= {"method": s["method"], "password": s["password"]}
    elif p == "hysteria2":
        out["password"] = s["password"]
        if s.get("obfs"):
            out["obfs"] = {"type": s["obfs"], "password": s.get("obfs_password", "")}
    if p != "ss":
        tls = _tls(s)
        if tls:
            out["tls"] = tls
        transport = _transport(s) if p != "hysteria2" else None
        if transport:
            out["transport"] = transport
    return out


def singbox_config(server: dict, bypass: list[str], out: dict | None = None) -> dict:
    """Весь трафик узла — в туннель; мимо: частные сети, адрес VPN-сервера и `bypass` (адрес панели и т.п.)."""
    exclude = list(PRIVATE_NETS)
    for item in [*bypass, server["host"]]:
        try:
            net = ipaddress.ip_network(item, strict=False)
            exclude.append(str(net))
        except ValueError:
            pass  # имя хоста — его адрес sing-box сам пустит напрямую через route ниже
    return {
        "log": {"level": "warn"},
        # Формат DNS sing-box 1.14: типизированные серверы; адрес VPN-сервера резолвим локально (иначе петля)
        "dns": {"servers": [{"type": "tls", "tag": "remote", "server": "1.1.1.1", "detour": "vpn"},
                            {"type": "local", "tag": "local"}],
                "rules": [{"domain": [server["host"]], "server": "local"}], "final": "remote"},
        "inbounds": [{"type": "tun", "tag": "tun-in", "interface_name": "hl-vpn", "address": ["172.19.0.1/30"],
                      "auto_route": True, "strict_route": False, "route_exclude_address": exclude, "stack": "system"}],
        "outbounds": [out or outbound(server), {"type": "direct", "tag": "direct"}],
        "route": {"rules": [{"action": "sniff"}, {"protocol": "dns", "action": "hijack-dns"},
                            {"ip_is_private": True, "outbound": "direct"},
                            {"domain": [server["host"]], "outbound": "direct"}],
                  "final": "vpn", "auto_detect_interface": True,
                  "default_domain_resolver": "local"},
    }


def valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s]+$", url)) and len(url) <= 2000


# ---------- Xray ----------

SOCKS_PORT = 10808
XRAY_TRANSPORTS = ("tcp", "", "raw", "none", "ws", "grpc", "httpupgrade", "xhttp")


def _xray_stream(s: dict, fingerprint: str) -> dict:
    t = s.get("transport", "tcp")
    if t not in XRAY_TRANSPORTS:
        raise SubscriptionError(f"транспорт {t} не поддерживается Xray")
    stream: dict = {"network": "tcp" if t in ("", "raw", "none") else t}
    host, path = s.get("host_header", ""), s.get("path") or "/"
    if t == "ws":
        stream["wsSettings"] = {"path": path, **({"headers": {"Host": host}} if host else {})}
    elif t == "grpc":
        stream["grpcSettings"] = {"serviceName": s.get("service_name") or s.get("path", "")}
    elif t == "httpupgrade":
        stream["httpupgradeSettings"] = {"path": path, "host": host}
    elif t == "xhttp":
        xh = {"path": path, "host": host, "mode": s.get("mode") or "auto"}
        if s.get("extra"):
            xh["extra"] = s["extra"]
        stream["xhttpSettings"] = xh
    sec = s.get("security")
    if sec in ("tls", "reality"):
        fp = fingerprint or s.get("fingerprint") or ("chrome" if sec == "reality" else "")
        stream["security"] = sec
        if sec == "tls":
            tls = {"serverName": s.get("sni") or s["host"], "allowInsecure": bool(s.get("insecure"))}
            if fp:
                tls["fingerprint"] = fp
            if s.get("alpn"):
                tls["alpn"] = [a for a in s["alpn"].split(",") if a]
            stream["tlsSettings"] = tls
        else:
            stream["realitySettings"] = {"serverName": s.get("sni") or s["host"], "fingerprint": fp,
                                         "publicKey": s.get("public_key", ""), "shortId": s.get("short_id", ""),
                                         "spiderX": s.get("spider_x", "")}
    return stream


def xray_config(server: dict, fingerprint: str = "", bypass: list[str] | None = None) -> dict:
    """Пара конфигов для узла с Xray: sing-box держит TUN и отдаёт трафик в локальный SOCKS Xray.
    Адрес сервера агент подставит IP-шником сам (до подъёма туннеля)."""
    p = server["protocol"]
    if p not in ("vless", "vmess", "trojan", "ss"):
        raise SubscriptionError(f"протокол {p} не поддерживается Xray")
    addr, port = server["host"], server["port"]
    if p == "vless":
        user = {"id": server["uuid"], "encryption": "none", **({"flow": server["flow"]} if server.get("flow") else {})}
        settings = {"vnext": [{"address": addr, "port": port, "users": [user]}]}
    elif p == "vmess":
        settings = {"vnext": [{"address": addr, "port": port, "users": [
            {"id": server["uuid"], "alterId": server.get("alter_id", 0), "security": "auto"}]}]}
    elif p == "trojan":
        settings = {"servers": [{"address": addr, "port": port, "password": server["password"]}]}
    else:
        settings = {"servers": [{"address": addr, "port": port, "method": server["method"],
                                 "password": server["password"]}]}
    proxy = {"tag": "proxy", "protocol": {"ss": "shadowsocks"}.get(p, p), "settings": settings}
    if p != "ss":
        proxy["streamSettings"] = _xray_stream(server, fingerprint)
    xray = {"log": {"loglevel": "warning"},
            "inbounds": [{"tag": "socks-in", "listen": "127.0.0.1", "port": SOCKS_PORT, "protocol": "socks",
                          "settings": {"auth": "noauth", "udp": True, "ip": "127.0.0.1"}}],
            "outbounds": [proxy, {"tag": "direct", "protocol": "freedom"}]}
    socks = {"type": "socks", "tag": "vpn", "server": "127.0.0.1", "server_port": SOCKS_PORT, "version": "5"}
    return {"singbox": singbox_config(server, bypass or [], out=socks), "xray": xray}


def engine_support(server: dict) -> tuple[bool, bool]:
    """(умеет sing-box, умеет Xray) для этого сервера."""
    try:
        singbox_config(server, [])
        sb = True
    except SubscriptionError:
        sb = False
    try:
        xray_config(server)
        xr = True
    except SubscriptionError:
        xr = False
    return sb, xr


def engine_configs(server: dict, fingerprint: str = "") -> tuple[dict | None, dict | None]:
    """→ (config sing-box | None, xray-пара | None). Не умеют оба — SubscriptionError."""
    if fingerprint:
        server = {**server, "fingerprint": fingerprint}
    try:
        config = singbox_config(server, [])
    except SubscriptionError:
        config = None
    try:
        xray = xray_config(server, fingerprint)
    except SubscriptionError:
        xray = None
    if config is None and xray is None:
        raise SubscriptionError(f"транспорт {server.get('transport')} / протокол {server['protocol']} не умеет "
                                f"ни sing-box, ни Xray")
    return config, xray
