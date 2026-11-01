"""Тип устройства servers.kind: server | pc | other

Revision ID: 0027_server_kind
Revises: 0025_ssh_host_keys
Create Date: 2026-11-02 00:30:00

Только ADD COLUMN: существующие записи становятся «server».
"""
from alembic import op
import sqlalchemy as sa


revision = '0027_server_kind'
down_revision = '0025_ssh_host_keys'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('servers', sa.Column('kind', sa.String(length=8), nullable=False, server_default='server'))


def downgrade() -> None:
    op.drop_column('servers', 'kind')
