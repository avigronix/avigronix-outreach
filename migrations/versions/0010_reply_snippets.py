"""reply snippets

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-18 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'reply_snippets',
        sa.Column('title', sa.String(length=100), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('reply_snippets')
