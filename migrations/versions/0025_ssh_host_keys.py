"""Ключи хостов SSH — отдельной таблицей по адресу и порту

Revision ID: 0025_ssh_host_keys
Revises: 0024_ssh_access
Create Date: 2026-11-01 23:00:00

Подключение вручную может идти на любой порт (как `ssh -p`), а на другом порту бывает другой sshd — ключ
нужен на пару адрес+порт. Запомненные ранее ключи переносятся; старые столбцы servers.host_key и
server_credentials.host_key остаются пустым грузом (без пересоздания таблиц), код их больше не читает.
"""
from alembic import op
import sqlalchemy as sa


revision = '0025_ssh_host_keys'
down_revision = '0024_ssh_access'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ssh_host_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('server_id', sa.Integer(), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('key_line', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('server_id', 'host', 'port'),
    )
    op.create_index('ix_ssh_host_keys_server_id', 'ssh_host_keys', ['server_id'])
    op.execute(
        "INSERT OR IGNORE INTO ssh_host_keys (server_id, host, port, key_line, created_at) "
        "SELECT id, host, ssh_port, host_key, CURRENT_TIMESTAMP FROM servers WHERE host_key != ''")
    op.execute(
        "INSERT OR IGNORE INTO ssh_host_keys (server_id, host, port, key_line, created_at) "
        "SELECT c.server_id, CASE WHEN c.host != '' THEN c.host ELSE s.host END, c.port, c.host_key, CURRENT_TIMESTAMP "
        "FROM server_credentials c JOIN servers s ON s.id = c.server_id WHERE c.host_key != ''")


def downgrade() -> None:
    op.drop_index('ix_ssh_host_keys_server_id', table_name='ssh_host_keys')
    op.drop_table('ssh_host_keys')
