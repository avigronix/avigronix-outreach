"""lead follow-up date

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-18 21:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('leads', sa.Column('follow_up_on', sa.Date(), nullable=True))
    op.create_index('ix_leads_follow_up_on', 'leads', ['follow_up_on'])


def downgrade() -> None:
    op.drop_index('ix_leads_follow_up_on', table_name='leads')
    op.drop_column('leads', 'follow_up_on')
