"""Сервисы: вид, веб-адрес, описание; права services.* и модуль «Сервисы» для существующих установок

Revision ID: 0030_service_web
Revises: 0028_merge_recordings_kind
Create Date: 2026-11-04 12:00:00

Только добавляет: колонки с значениями по умолчанию, права services.view/edit группам, у которых
уже есть servers.view/edit, и «services» в сохранённый список включённых разделов (иначе раздел
у существующих установок был бы выключен).
"""
import json

from alembic import op
import sqlalchemy as sa


revision = '0030_service_web'
down_revision = '0028_merge_recordings_kind'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('services', sa.Column('kind', sa.String(length=32), nullable=False, server_default='generic'))
    op.add_column('services', sa.Column('web_url', sa.String(length=500), nullable=False, server_default=''))
    op.add_column('services', sa.Column('description', sa.Text(), nullable=False, server_default=''))

    bind = op.get_bind()
    # Новые права — тем группам, у кого есть соответствующие права на серверы (администраторам, пользователям, наблюдателям)
    for old, new in (("servers.view", "services.view"), ("servers.edit", "services.edit")):
        group_ids = [r[0] for r in bind.execute(sa.text("SELECT group_id FROM group_permissions WHERE perm = :p"), {"p": old})]
        for gid in group_ids:
            exists = bind.execute(sa.text("SELECT 1 FROM group_permissions WHERE group_id = :g AND perm = :p"),
                                  {"g": gid, "p": new}).first()
            if not exists:
                bind.execute(sa.text("INSERT INTO group_permissions (group_id, perm) VALUES (:g, :p)"), {"g": gid, "p": new})

    row = bind.execute(sa.text("SELECT id, value FROM setting_values WHERE key = 'modules.enabled' AND user_id IS NULL")).first()
    if row:
        modules = json.loads(row[1])
        if "services" not in modules:
            bind.execute(sa.text("UPDATE setting_values SET value = :v WHERE id = :i"),
                         {"v": json.dumps(modules + ["services"]), "i": row[0]})


def downgrade() -> None:
    op.drop_column('services', 'description')
    op.drop_column('services', 'web_url')
    op.drop_column('services', 'kind')
