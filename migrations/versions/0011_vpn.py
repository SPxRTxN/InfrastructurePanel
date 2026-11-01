"""vpn: subscriptions, assignments

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-01 15:00:00

Только новые таблицы.
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('vpn_subscriptions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('url_enc', sa.Text(), nullable=False),
    sa.Column('insecure', sa.Boolean(), nullable=False),
    sa.Column('servers_enc', sa.Text(), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('fetched_at', sa.DateTime(), nullable=True),
    sa.Column('error', sa.String(length=500), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('vpn_assignments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.Integer(), nullable=False),
    sa.Column('subscription_id', sa.Integer(), nullable=False),
    sa.Column('server_key', sa.String(length=16), nullable=False),
    sa.Column('server_name', sa.String(length=128), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('detail', sa.String(length=500), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['subscription_id'], ['vpn_subscriptions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('node_id')
    )


def downgrade() -> None:
    op.drop_table('vpn_assignments')
    op.drop_table('vpn_subscriptions')
