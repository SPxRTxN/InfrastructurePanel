"""backups: журнал бэкапов и проверок восстановления

Revision ID: 0013_backups
Revises: 0011
Create Date: 2026-11-02 12:00:00

Только новая таблица. Вторая ветка от 0011: слияние голов — отдельной миграцией.
"""
from alembic import op
import sqlalchemy as sa


revision = '0013_backups'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('backup_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ts', sa.DateTime(), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('trigger', sa.String(length=8), nullable=False),
    sa.Column('file', sa.String(length=255), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('error', sa.String(length=500), nullable=False),
    sa.Column('verified', sa.Boolean(), nullable=False),
    sa.Column('verify_ok', sa.Boolean(), nullable=True),
    sa.Column('verify_detail', sa.String(length=500), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('backup_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_backup_log_ts'), ['ts'], unique=False)


def downgrade() -> None:
    op.drop_table('backup_log')
