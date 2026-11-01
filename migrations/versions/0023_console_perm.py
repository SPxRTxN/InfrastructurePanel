"""Право servers.console (SSH-консоль в браузере) — встроенной группе «Администраторы»

Revision ID: 0023_console_perm
Revises: 0022_node_tombstones
Create Date: 2026-11-01 18:00:00

Только данные: ensure_default_groups права существующих групп не меняет, поэтому администраторам рабочей БД
право добавляется здесь. Остальным группам — вручную (Настройки → Группы и права).
"""
from alembic import op
import sqlalchemy as sa


revision = '0023_console_perm'
down_revision = '0022_node_tombstones'
branch_labels = None
depends_on = None

PERM = 'servers.console'


def upgrade() -> None:
    bind = op.get_bind()
    group_id = bind.execute(sa.text("SELECT id FROM groups WHERE name = :n AND builtin = 1"),
                            {"n": "Администраторы"}).scalar()
    if group_id is None:
        return
    exists = bind.execute(sa.text("SELECT 1 FROM group_permissions WHERE group_id = :g AND perm = :p"),
                          {"g": group_id, "p": PERM}).scalar()
    if not exists:
        bind.execute(sa.text("INSERT INTO group_permissions (group_id, perm) VALUES (:g, :p)"),
                     {"g": group_id, "p": PERM})


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM group_permissions WHERE perm = :p"), {"p": PERM})
