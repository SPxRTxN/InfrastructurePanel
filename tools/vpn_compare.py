"""Сравнить VPN-сервер, назначенный узлу в панели, с рабочим конфигом Xray (JSON на stdin) — без вывода значений.

Запуск на сервере панели (конфиг Xray читает root, внутри контейнера — только сравнение, наружу — «совпадает»/«НЕ»):
  sudo bash -c 'cd /opt/homelab-dashboard && docker compose exec -T dashboard python -c "$(cat tools/vpn_compare.py)" < <путь к config.json xray>'
"""
import json
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models.vpn import VpnAssignment
from app.services import vpn


def xray_outbounds(cfg: dict) -> list[dict]:
    """vless-outbound'ы Xray в плоском виде (старый формат vnext/users и новый — поля прямо в settings)."""
    found = []
    for ob in cfg.get("outbounds", []):
        if ob.get("protocol") != "vless":
            continue
        st, ss = ob.get("settings", {}), ob.get("streamSettings", {})
        peer = (st.get("vnext") or [st])[0]
        user = (peer.get("users") or [peer])[0]
        rs = ss.get("realitySettings", {})
        found.append({"tag": ob.get("tag", "?"), "host": peer.get("address", ""), "port": int(peer.get("port", 0) or 0),
                      "uuid": user.get("id", ""), "flow": user.get("flow", ""),
                      "transport": ss.get("network", "tcp"), "security": ss.get("security", "none"),
                      "sni": rs.get("serverName", ""), "fingerprint": rs.get("fingerprint", ""),
                      # Xray 25+: публичный ключ клиента называется password
                      "public_key": rs.get("publicKey", "") or rs.get("password", ""), "short_id": rs.get("shortId", "")})
    return found


FIELDS = ("host", "port", "uuid", "flow", "transport", "security", "sni", "fingerprint", "public_key", "short_id")


def main() -> int:
    ref = xray_outbounds(json.load(sys.stdin))
    if not ref:
        print("в конфиге Xray нет vless-outbound")
        return 1
    with SessionLocal() as db:
        for a in db.scalars(select(VpnAssignment)).all():
            server = next((s for s in vpn.unpack(a.subscription.servers_enc) if s["key"] == a.server_key), None)
            if not server:
                continue
            print(f"Узел {a.node.name} ↔ Xray:")
            for x in ref:
                same = [f for f in FIELDS if str(server.get(f, "")) == str(x[f])]
                print(f"  outbound «{x['tag']}»: совпадает {len(same)} из {len(FIELDS)}")
                for f in FIELDS:
                    print(f"    {f}: {'совпадает' if f in same else 'НЕ совпадает'}")
    return 0


sys.exit(main())
