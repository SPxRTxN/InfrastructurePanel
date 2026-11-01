"""merge: 0015_merge + 0013_backups

Revision ID: 0016_merge
Revises: 0015_merge, 0013_backups
Create Date: 2026-11-02 01:30:00

Слияние веток миграций от параллельной разработки; схему не меняет.
"""

revision = '0016_merge'
down_revision = ('0015_merge', '0013_backups')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
