"""Ключ хоста SSH у сервера и сохранённые входы (ОС / BMC)

Revision ID: 0024_ssh_access
Revises: 0023_console_perm
Create Date: 2026-11-01 20:00:00

servers.host_key — простой ADD COLUMN (таблица servers не пересоздаётся); server_credentials — новая таблица.
"""
from alembic import op
import sqlalchemy as sa


revision = '0024_ssh_access'
down_revision = '0023_console_perm'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('servers', sa.Column('host_key', sa.Text(), nullable=False, server_default=''))
    op.create_table(
        'server_credentials',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('label', sa.String(length=64), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('password_enc', sa.Text(), nullable=False),
        sa.Column('host_key', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_server_credentials_server_id', 'server_credentials', ['server_id'])


def downgrade() -> None:
    op.drop_index('ix_server_credentials_server_id', table_name='server_credentials')
    op.drop_table('server_credentials')
    with op.batch_alter_table('servers', schema=None) as batch_op:
        batch_op.drop_column('host_key')
