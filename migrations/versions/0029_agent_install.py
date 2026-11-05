"""agent install: одноразовые коды установки агента и ход установки

Revision ID: 0029_agent_install
Revises: 0028_merge_recordings_kind
Create Date: 2026-11-04 10:00:00

Только новая таблица.
"""
from alembic import op
import sqlalchemy as sa


revision = '0029_agent_install'
down_revision = '0028_merge_recordings_kind'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('agent_installs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.Integer(), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('vpn', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('token_issued_at', sa.DateTime(), nullable=True),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('steps_json', sa.Text(), nullable=False),
    sa.Column('log', sa.Text(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code_hash')
    )
    op.create_index(op.f('ix_agent_installs_node_id'), 'agent_installs', ['node_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_agent_installs_node_id'), table_name='agent_installs')
    op.drop_table('agent_installs')
