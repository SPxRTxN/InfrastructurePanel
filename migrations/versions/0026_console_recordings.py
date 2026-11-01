"""Записи сеансов SSH-консоли

Revision ID: 0026_console_recordings
Revises: 0025_ssh_host_keys
Create Date: 2026-11-01 21:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0026_console_recordings'
down_revision = '0025_ssh_host_keys'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'console_recordings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('server_id', sa.Integer(), sa.ForeignKey('servers.id', ondelete='SET NULL')),
        sa.Column('server_name', sa.String(120), nullable=False, server_default=''),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
        sa.Column('username', sa.String(64), nullable=False, server_default=''),
        sa.Column('target', sa.String(255), nullable=False, server_default=''),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('ended_at', sa.DateTime(), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('pinned', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('reason', sa.String(120), nullable=False, server_default=''),
    )
    op.create_index('ix_console_recordings_server_id', 'console_recordings', ['server_id'])


def downgrade() -> None:
    op.drop_index('ix_console_recordings_server_id', 'console_recordings')
    op.drop_table('console_recordings')
