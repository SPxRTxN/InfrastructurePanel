"""alert_rule_groups: получатели колокольчика по группам

Revision ID: 0012_alert_groups
Revises: 0011
Create Date: 2026-11-02 10:00:00

Только новая таблица.
"""
from alembic import op
import sqlalchemy as sa


revision = '0012_alert_groups'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('alert_rule_groups',
    sa.Column('rule_id', sa.Integer(), nullable=False),
    sa.Column('group_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['rule_id'], ['alert_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('rule_id', 'group_id')
    )


def downgrade() -> None:
    op.drop_table('alert_rule_groups')
