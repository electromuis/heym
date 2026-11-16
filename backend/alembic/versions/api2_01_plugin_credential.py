"""add the plugin credential type (Plugin API 2)

A package's own credential types are all stored as type `plugin`; the config names the
package and the type (app/services/plugin_credentials.py).

Revision ID: api2_01_plugin_credential
Revises: 156_gmail_credential
Create Date: 2026-11-16 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "api2_01_plugin_credential"
down_revision: str | Sequence[str] | None = "156_gmail_credential"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE credential_type ADD VALUE IF NOT EXISTS 'plugin'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely without rebuilding the type.
    pass
