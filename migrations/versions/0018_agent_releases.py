"""agent releases: каталог версий агента и журнал обновлений узлов

Revision ID: 0018_agent_releases
Revises: 0016_merge
Create Date: 2026-11-01 20:00:00

Только новые таблицы.
"""
from alembic import op
import sqlalchemy as sa


revision = '0018_agent_releases'
down_revision = '0016_merge'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('agent_releases',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('version', sa.String(length=32), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('notes', sa.String(length=500), nullable=False),
    sa.Column('uploaded_by', sa.Integer(), nullable=True),
    sa.Column('uploaded_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('version')
    )
    op.create_table('node_updates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.Integer(), nullable=False),
    sa.Column('from_version', sa.String(length=32), nullable=False),
    sa.Column('to_version', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('detail', sa.String(length=500), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('requested_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_node_updates_node_id'), 'node_updates', ['node_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_node_updates_node_id'), table_name='node_updates')
    op.drop_table('node_updates')
    op.drop_table('agent_releases')
