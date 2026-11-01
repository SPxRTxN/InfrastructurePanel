"""nodes: agents, samples, actions, events

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-31 15:00:00

Только новые таблицы — существующие не пересоздаются, данные не затрагиваются.
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('nodes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('server_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('connected', sa.Boolean(), nullable=False),
    sa.Column('last_seen', sa.DateTime(), nullable=True),
    sa.Column('version', sa.String(length=32), nullable=False),
    sa.Column('remote_ip', sa.String(length=64), nullable=False),
    sa.Column('info_json', sa.Text(), nullable=False),
    sa.Column('sensors_json', sa.Text(), nullable=False),
    sa.Column('actions_json', sa.Text(), nullable=False),
    sa.Column('ips_json', sa.Text(), nullable=False),
    sa.Column('values_json', sa.Text(), nullable=False),
    sa.Column('values_ts', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['server_id'], ['servers.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_nodes_server_id'), ['server_id'], unique=False)

    op.create_table('node_samples',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.Integer(), nullable=False),
    sa.Column('ts', sa.DateTime(), nullable=False),
    sa.Column('values_json', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('node_samples', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_node_samples_node_id'), ['node_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_node_samples_ts'), ['ts'], unique=False)

    op.create_table('node_actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('requested_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('code', sa.Integer(), nullable=True),
    sa.Column('output', sa.Text(), nullable=False),
    sa.Column('error', sa.String(length=200), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('node_actions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_node_actions_node_id'), ['node_id'], unique=False)

    op.create_table('node_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.Integer(), nullable=False),
    sa.Column('ts', sa.DateTime(), nullable=False),
    sa.Column('level', sa.String(length=16), nullable=False),
    sa.Column('text', sa.String(length=500), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('node_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_node_events_node_id'), ['node_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_node_events_ts'), ['ts'], unique=False)


def downgrade() -> None:
    op.drop_table('node_events')
    op.drop_table('node_actions')
    op.drop_table('node_samples')
    op.drop_table('nodes')
