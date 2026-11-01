"""maintenance: режим обслуживания на время обновления панели

Revision ID: 0017_maintenance
Revises: 0016_merge
Create Date: 2026-11-03 10:00:00

Только новая таблица.
"""
from alembic import op
import sqlalchemy as sa


revision = '0017_maintenance'
down_revision = '0016_merge'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('maintenance',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('since', sa.DateTime(), nullable=False),
    sa.Column('eta', sa.DateTime(), nullable=True),
    sa.Column('by', sa.String(length=64), nullable=False),
    sa.Column('message', sa.String(length=255), nullable=False),
    sa.Column('target_version', sa.String(length=32), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('maintenance')
