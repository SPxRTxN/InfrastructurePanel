"""Слияние веток миграций: записи консоли (0026) и тип устройства сервера (0027)

Revision ID: 0028_merge_recordings_kind
Revises: 0026_console_recordings, 0027_server_kind
Create Date: 2026-11-02 02:00:00
"""

revision = '0028_merge_recordings_kind'
down_revision = ('0026_console_recordings', '0027_server_kind')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
