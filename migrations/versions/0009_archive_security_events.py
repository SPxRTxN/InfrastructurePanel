"""archives, security_events, sessions.user_agent

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-31 20:00:00

Новые таблицы и одна колонка с server_default: ADD COLUMN без пересоздания sessions.
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('archives',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('summary', sa.String(length=500), nullable=False),
    sa.Column('deleted_by', sa.String(length=64), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('archives', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_archives_deleted_at'), ['deleted_at'], unique=False)

    op.create_table('security_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ts', sa.DateTime(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=False),
    sa.Column('user_agent', sa.String(length=255), nullable=False),
    sa.Column('detail', sa.String(length=500), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('security_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_security_events_ts'), ['ts'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_events_kind'), ['kind'], unique=False)

    # Простой ADD COLUMN (не batch): таблица sessions не пересоздаётся
    op.add_column('sessions', sa.Column('user_agent', sa.String(length=255), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_column('user_agent')
    op.drop_table('security_events')
    op.drop_table('archives')
