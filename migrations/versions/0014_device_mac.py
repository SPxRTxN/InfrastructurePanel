"""device mac

Revision ID: 0014_device_mac
Revises: 0011
Create Date: 2026-11-02 10:00:00

Только добавляет колонку devices.mac (для Wake-on-LAN).
"""
from alembic import op
import sqlalchemy as sa


revision = '0014_device_mac'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('devices', sa.Column('mac', sa.String(length=17), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('devices') as batch:
        batch.drop_column('mac')
