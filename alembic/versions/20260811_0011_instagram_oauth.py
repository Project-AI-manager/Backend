"""Add durable Instagram OAuth attempts.

Revision ID: 20260811_0011
Revises: 20260808_0010
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260811_0011"
down_revision: str | None = "20260808_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instagram_oauth_attempt",
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("browser_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("replace_channel_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["replace_channel_id"], ["channel.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash", name="uq_instagram_oauth_attempt_state_hash"),
    )
    op.create_index(
        "ix_instagram_oauth_attempt_tenant_id", "instagram_oauth_attempt", ["tenant_id"]
    )
    op.create_index(
        "ix_instagram_oauth_attempt_user_id", "instagram_oauth_attempt", ["user_id"]
    )
    op.create_index(
        "ix_instagram_oauth_attempt_expires_at", "instagram_oauth_attempt", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_instagram_oauth_attempt_expires_at", table_name="instagram_oauth_attempt")
    op.drop_index("ix_instagram_oauth_attempt_user_id", table_name="instagram_oauth_attempt")
    op.drop_index("ix_instagram_oauth_attempt_tenant_id", table_name="instagram_oauth_attempt")
    op.drop_table("instagram_oauth_attempt")
