"""sessions.last_seen — тайм-аут бездействия

Revision ID: 0021_session_idle
Revises: 0020_topology_v2
Create Date: 2026-11-02 10:00:00

Простой ADD COLUMN (nullable): таблица sessions не пересоздаётся.
"""
from alembic import op
import sqlalchemy as sa


revision = '0021_session_idle'
down_revision = '0020_topology_v2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('sessions', sa.Column('last_seen', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_column('last_seen')
