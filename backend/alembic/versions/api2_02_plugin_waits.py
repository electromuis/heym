"""add plugin_waits (Plugin API 2: ctx.wait pauses a run until something outside answers)

Revision ID: api2_02_plugin_waits
Revises: api2_01_plugin_credential
Create Date: 2026-11-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "api2_02_plugin_waits"
down_revision: str | Sequence[str] | None = "api2_01_plugin_credential"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plugin_waits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("plugin_id", sa.String(255), nullable=False),
        sa.Column("wait_key", sa.String(255), nullable=False),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "execution_history_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("execution_history.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("node_label", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=True),
        sa.Column("execution_snapshot", postgresql.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("resume_body", postgresql.JSON(), nullable=False),
        sa.Column("resume_error", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
    )
    op.create_index("ix_plugin_waits_plugin_id", "plugin_waits", ["plugin_id"])
    op.create_index("ix_plugin_waits_workflow_id", "plugin_waits", ["workflow_id"])
    op.create_index(
        "ix_plugin_waits_execution_history_id", "plugin_waits", ["execution_history_id"]
    )
    op.create_index("ix_plugin_waits_status", "plugin_waits", ["status"])
    op.create_index("ix_plugin_waits_expires_at", "plugin_waits", ["expires_at"])
    op.create_index(
        "uq_plugin_waits_pending_key",
        "plugin_waits",
        ["plugin_id", "wait_key"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("plugin_waits")
