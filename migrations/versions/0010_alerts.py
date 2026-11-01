"""alerts: rules, states, log

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-01 12:00:00

Только новые таблицы.
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('alert_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('threshold', sa.Float(), nullable=True),
    sa.Column('sensor', sa.String(length=200), nullable=False),
    sa.Column('op', sa.String(length=2), nullable=False),
    sa.Column('target', sa.String(length=128), nullable=False),
    sa.Column('for_minutes', sa.Integer(), nullable=False),
    sa.Column('repeat_minutes', sa.Integer(), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('notify_bell', sa.Boolean(), nullable=False),
    sa.Column('notify_telegram', sa.Boolean(), nullable=False),
    sa.Column('notify_webhook', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('alert_states',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('rule_id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=300), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('link', sa.String(length=255), nullable=False),
    sa.Column('value', sa.String(length=64), nullable=False),
    sa.Column('pending_since', sa.DateTime(), nullable=True),
    sa.Column('firing', sa.Boolean(), nullable=False),
    sa.Column('fired_at', sa.DateTime(), nullable=True),
    sa.Column('last_sent', sa.DateTime(), nullable=True),
    sa.Column('seen_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['rule_id'], ['alert_rules.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('rule_id', 'key')
    )
    with op.batch_alter_table('alert_states', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alert_states_rule_id'), ['rule_id'], unique=False)

    op.create_table('alert_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ts', sa.DateTime(), nullable=False),
    sa.Column('rule_name', sa.String(length=128), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('value', sa.String(length=64), nullable=False),
    sa.Column('link', sa.String(length=255), nullable=False),
    sa.Column('delivered', sa.String(length=100), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alert_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alert_log_ts'), ['ts'], unique=False)


def downgrade() -> None:
    op.drop_table('alert_log')
    op.drop_table('alert_states')
    op.drop_table('alert_rules')
