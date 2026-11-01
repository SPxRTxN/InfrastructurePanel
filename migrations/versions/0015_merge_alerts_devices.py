"""merge: 0012_alert_groups + 0014_device_mac

Revision ID: 0015_merge
Revises: 0012_alert_groups, 0014_device_mac
Create Date: 2026-11-02 01:00:00

Слияние веток миграций от параллельной разработки; схему не меняет.
"""

revision = '0015_merge'
down_revision = ('0012_alert_groups', '0014_device_mac')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
