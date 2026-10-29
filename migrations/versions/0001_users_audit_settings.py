"""users, audit_logs, settings

Revision ID: 0001
Revises:
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "users",
        *timestamps(),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("session_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("role IN ('admin', 'manager', 'sales')", name="ck_users_role"),
    )

    op.create_table(
        "audit_logs",
        *timestamps(),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("entity", sa.String(40)),
        sa.Column("entity_id", sa.BigInteger),
        sa.Column("ip", sa.String(64)),
        sa.Column("meta", JSONB, nullable=False, server_default="{}"),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    op.create_table(
        "settings",
        *timestamps(),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("postal_address", sa.Text, nullable=False, server_default=""),
        sa.Column("cap_day_tz", sa.String(64), nullable=False, server_default="America/New_York"),
        sa.Column("bounce_pause_pct", sa.Float, nullable=False, server_default="3.0"),
        sa.Column("complaint_pause_pct", sa.Float, nullable=False, server_default="0.1"),
        sa.Column("pause_min_sends", sa.Integer, nullable=False, server_default="50"),
        sa.Column("domain_daily_max", sa.Integer, nullable=False, server_default="3"),
        sa.Column("worker_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_daily_run", sa.DateTime(timezone=True)),
    )
    op.execute("INSERT INTO settings (id, company_name) VALUES (1, 'Avigronix Technologies')")


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("users")
