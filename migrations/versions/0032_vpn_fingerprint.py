"""VPN: отпечаток uTLS у назначения узлу («» — как в подписке)

Revision ID: 0032_vpn_fingerprint
Revises: 0031_merge_install_services
Create Date: 2026-11-04 20:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0032_vpn_fingerprint'
down_revision = '0031_merge_install_services'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('vpn_assignments', sa.Column('fingerprint', sa.String(length=16), nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('vpn_assignments', 'fingerprint')
