"""merge: 0017_maintenance + 0018_agent_releases

Revision ID: 0019_merge
Revises: 0017_maintenance, 0018_agent_releases
Create Date: 2026-11-02 04:00:00

Слияние веток миграций волны 2; схему не меняет.
"""

revision = '0019_merge'
down_revision = ('0017_maintenance', '0018_agent_releases')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
