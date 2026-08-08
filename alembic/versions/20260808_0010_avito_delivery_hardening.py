"""Harden Avito OAuth, webhook routing and background claims.

Revision ID: 20260808_0010
Revises: 20260808_0009
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0010"
down_revision: str | None = "20260808_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("channel") as batch_op:
        batch_op.add_column(sa.Column("webhook_identity", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_channel_webhook_identity", ["webhook_identity"], unique=True)

    with op.batch_alter_table("webhook_event") as batch_op:
        batch_op.add_column(
            sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            "ix_webhook_event_processing_started_at", ["processing_started_at"], unique=False
        )

    op.create_table(
        "avito_oauth_attempt",
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("browser_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash", name="uq_avito_oauth_attempt_state_hash"),
    )
    op.create_index("ix_avito_oauth_attempt_tenant_id", "avito_oauth_attempt", ["tenant_id"])
    op.create_index("ix_avito_oauth_attempt_user_id", "avito_oauth_attempt", ["user_id"])
    op.create_index("ix_avito_oauth_attempt_expires_at", "avito_oauth_attempt", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_avito_oauth_attempt_expires_at", table_name="avito_oauth_attempt")
    op.drop_index("ix_avito_oauth_attempt_user_id", table_name="avito_oauth_attempt")
    op.drop_index("ix_avito_oauth_attempt_tenant_id", table_name="avito_oauth_attempt")
    op.drop_table("avito_oauth_attempt")

    with op.batch_alter_table("webhook_event") as batch_op:
        batch_op.drop_index("ix_webhook_event_processing_started_at")
        batch_op.drop_column("processing_started_at")

    with op.batch_alter_table("channel") as batch_op:
        batch_op.drop_index("ix_channel_webhook_identity")
        batch_op.drop_column("webhook_identity")
