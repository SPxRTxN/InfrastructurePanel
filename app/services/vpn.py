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
               "sni": str(data.get("sni", "") or data.get("host", ""))}
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
                   "service_name": q.get("serviceName", ""), "insecure": q.get("insecure", q.get("allowInsecure", "0")) in ("1", "true")}
            if scheme == "vless":
                srv |= {"uuid": secret, "flow": q.get("flow", ""), "public_key": q.get("pbk", ""), "short_id": q.get("sid", "")}
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


def pack(servers: list[dict]) -> str:
    return encrypt(json.dumps(servers, ensure_ascii=False))


def unpack(blob: str) -> list[dict]:
    return json.loads(decrypt(blob) or "[]") if blob else []


# ---------- Конфиг клиента для узла (sing-box) ----------

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


def singbox_config(server: dict, bypass: list[str]) -> dict:
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
        "dns": {"servers": [{"tag": "remote", "address": "tls://1.1.1.1", "detour": "vpn"},
                            {"tag": "local", "address": "local", "detour": "direct"}],
                "rules": [{"server": "local", "domain": [server["host"]]}], "final": "remote"},
        "inbounds": [{"type": "tun", "tag": "tun-in", "interface_name": "hl-vpn", "address": ["172.19.0.1/30"],
                      "auto_route": True, "strict_route": False, "route_exclude_address": exclude, "stack": "system"}],
        "outbounds": [outbound(server), {"type": "direct", "tag": "direct"}],
        "route": {"rules": [{"action": "sniff"}, {"protocol": "dns", "action": "hijack-dns"},
                            {"ip_is_private": True, "outbound": "direct"},
                            {"domain": [server["host"]], "outbound": "direct"}],
                  "final": "vpn", "auto_detect_interface": True},
    }


def valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s]+$", url)) and len(url) <= 2000
