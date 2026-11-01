"""topology v2: порты, Wi-Fi-связи, сегменты

Revision ID: 0020_topology_v2
Revises: 0019_merge
Create Date: 2026-11-02 12:00:00

Только добавляет: devices.ports, devices.segment_id (без FK — SET NULL делает код),
links.kind и таблицу segments. Существующие таблицы не пересоздаются.
"""
from alembic import op
import sqlalchemy as sa


revision = '0020_topology_v2'
down_revision = '0019_merge'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('devices', sa.Column('ports', sa.Text(), nullable=False, server_default=''))
    op.add_column('devices', sa.Column('segment_id', sa.Integer(), nullable=True))
    op.create_index('ix_devices_segment_id', 'devices', ['segment_id'])
    op.add_column('links', sa.Column('kind', sa.String(length=8), nullable=False, server_default='wired'))
    op.create_table(
        'segments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('color', sa.String(length=7), nullable=False, server_default='#3b82f6'),
        sa.Column('subnet', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('description', sa.String(length=500), nullable=False, server_default=''),
    )


def downgrade() -> None:
    op.drop_table('segments')
    with op.batch_alter_table('links') as batch:
        batch.drop_column('kind')
    with op.batch_alter_table('devices') as batch:
        batch.drop_index('ix_devices_segment_id')
        batch.drop_column('segment_id')
        batch.drop_column('ports')
