"""node_tombstones — «надгробия» удалённых узлов (удаление агента при возврате на связь)

Revision ID: 0022_node_tombstones
Revises: 0021_session_idle
Create Date: 2026-11-02 12:00:00

Только новая таблица.
"""
from alembic import op
import sqlalchemy as sa


revision = '0022_node_tombstones'
down_revision = '0021_session_idle'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'node_tombstones',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('token_hash', sa.String(length=128), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_node_tombstones_token_hash'), 'node_tombstones', ['token_hash'])
    op.create_index(op.f('ix_node_tombstones_expires_at'), 'node_tombstones', ['expires_at'])


def downgrade() -> None:
    op.drop_table('node_tombstones')
