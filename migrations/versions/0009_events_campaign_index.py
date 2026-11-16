"""events campaign index

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-16 12:00:00.000000
"""

from alembic import op


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Campaign page and reports filter events by campaign_id.
    op.create_index(op.f('ix_events_campaign_id'), 'events', ['campaign_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_events_campaign_id'), table_name='events')
