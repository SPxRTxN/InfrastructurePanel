"""Установка агента: свой Python (--own-python) — отдельно от системного

Revision ID: 0034_agent_install_python
Revises: 0033_vpn_engine
Create Date: 2026-11-04 23:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0034_agent_install_python'
down_revision = '0033_vpn_engine'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('agent_installs', sa.Column('own_python', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('agent_installs', 'own_python')
