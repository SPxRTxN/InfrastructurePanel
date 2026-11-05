"""Слияние веток миграций: установка агента (0029) и раздел «Сервисы» (0030)

Revision ID: 0031_merge_install_services
Revises: 0029_agent_install, 0030_service_web
Create Date: 2026-11-04 04:00:00
"""

revision = '0031_merge_install_services'
down_revision = ('0029_agent_install', '0030_service_web')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
