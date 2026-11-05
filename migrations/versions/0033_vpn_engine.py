"""VPN: движок, на котором поднялось назначение (sing-box / xray)

Revision ID: 0033_vpn_engine
Revises: 0032_vpn_fingerprint
Create Date: 2026-11-04 22:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0033_vpn_engine'
down_revision = '0032_vpn_fingerprint'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('vpn_assignments', sa.Column('engine', sa.String(length=16), nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('vpn_assignments', 'engine')
