"""Миграции не теряют данные существующей БД.

Регрессия 2026-10-31: 0007 (batch-режим пересоздаёт `users`) при PRAGMA foreign_keys=ON каскадно
стёрла user_groups, sessions и setting_values рабочей БД — тесты на пустой БД этого не видели.
"""
import sqlite3

from alembic import command
from alembic.script import ScriptDirectory

from app import migrate

ROWS = {
    "groups": "INSERT INTO groups (id, name, description, builtin) VALUES (1, 'Администраторы', '', 1)",
    "group_permissions": "INSERT INTO group_permissions (group_id, perm) VALUES (1, 'users.manage')",
    "users": "INSERT INTO users (id, username, display_name, password_hash, is_active, created_at) "
             "VALUES (1, 'owner', '', 'x', 1, '2026-10-30 00:00:00')",
    "user_groups": "INSERT INTO user_groups (user_id, group_id) VALUES (1, 1)",
    "sessions": "INSERT INTO sessions (token_hash, csrf_token, user_id, created_at, expires_at, ip) "
                "VALUES ('h', 'c', 1, '2026-10-30 00:00:00', '2099-01-01 00:00:00', '')",
    "setting_values": "INSERT INTO setting_values (key, user_id, value) VALUES ('appearance.theme', 1, '\"light\"')",
    "notifications": "INSERT INTO notifications (user_id, title, body, link, is_read, created_at) "
                     "VALUES (1, 't', '', '', 0, '2026-10-30 00:00:00')",
}


def test_0025_moves_host_keys(tmp_path):
    """Ключи, запомненные до 0025 (servers.host_key, server_credentials.host_key), переезжают в ssh_host_keys."""
    url = f"sqlite:///{tmp_path / 'keys.db'}"
    command.upgrade(migrate.alembic_config(url), "0024_ssh_access")
    with sqlite3.connect(tmp_path / "keys.db") as c:
        c.execute("INSERT INTO servers (id, name, host, ssh_port, collector, ssh_user, ssh_key_enc, host_key, enabled, "
                  "last_error) VALUES (1, 'pi', '10.0.0.5', 2222, 'ssh', 'u', '', 'ssh-ed25519 SRV', 1, '')")
        c.execute("INSERT INTO server_credentials (server_id, kind, label, host, port, username, password_enc, "
                  "host_key, created_at) VALUES (1, 'bmc', '', '10.0.0.6', 22, 'root', '', 'ssh-ed25519 BMC', "
                  "'2026-11-01 00:00:00'), (1, 'os', '', '', 2222, 'u', '', '', '2026-11-01 00:00:00')")
    migrate.upgrade(url)
    with sqlite3.connect(tmp_path / "keys.db") as c:
        rows = set(c.execute("SELECT server_id, host, port, key_line FROM ssh_host_keys"))
    assert rows == {(1, "10.0.0.5", 2222, "ssh-ed25519 SRV"), (1, "10.0.0.6", 22, "ssh-ed25519 BMC")}


def test_upgrade_from_0006_keeps_related_rows(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    command.upgrade(migrate.alembic_config(url), "0006")
    with sqlite3.connect(tmp_path / "old.db") as c:
        for sql in ROWS.values():
            c.execute(sql)

    migrate.upgrade(url)  # → head, в т.ч. 0007 с пересозданием users

    with sqlite3.connect(tmp_path / "old.db") as c:
        head = ScriptDirectory.from_config(migrate.alembic_config(url)).get_current_head()
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head
        assert c.execute("SELECT user_agent FROM sessions").fetchone()[0] == ""  # 0009: колонка добавлена, строка цела
        for table in set(ROWS) - {"group_permissions"}:
            assert c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1, table  # noqa: S608
        # Права группы целы, 0023 добавила администраторам SSH-консоль
        perms = {r[0] for r in c.execute("SELECT perm FROM group_permissions WHERE group_id = 1")}
        assert perms == {"users.manage", "servers.console"}
        assert c.execute("SELECT must_change_credentials FROM users").fetchone()[0] == 0
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
