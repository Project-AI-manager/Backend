"""Add transactional email module.

Revision ID: 20260704_0002
Revises: 20260623_0001
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260704_0002"
down_revision: str | None = "20260623_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def uuid_pk() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False)


def timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "email_token",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("user.id"), nullable=False
        ),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
    )
    op.create_index("ix_email_token_tenant_id", "email_token", ["tenant_id"])
    op.create_index("ix_email_token_user_id", "email_token", ["user_id"])
    op.create_index("ix_email_token_purpose", "email_token", ["purpose"])
    op.create_index("ix_email_token_token_hash", "email_token", ["token_hash"])
    op.create_index("ix_email_token_email", "email_token", ["email"])

    op.create_table(
        "email_outbox",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=True
        ),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("user.id"), nullable=True
        ),
        sa.Column("to_email", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_email_outbox_tenant_id", "email_outbox", ["tenant_id"])
    op.create_index("ix_email_outbox_user_id", "email_outbox", ["user_id"])
    op.create_index("ix_email_outbox_to_email", "email_outbox", ["to_email"])
    op.create_index("ix_email_outbox_purpose", "email_outbox", ["purpose"])


def downgrade() -> None:
    op.drop_index("ix_email_outbox_purpose", table_name="email_outbox")
    op.drop_index("ix_email_outbox_to_email", table_name="email_outbox")
    op.drop_index("ix_email_outbox_user_id", table_name="email_outbox")
    op.drop_index("ix_email_outbox_tenant_id", table_name="email_outbox")
    op.drop_table("email_outbox")

    op.drop_index("ix_email_token_email", table_name="email_token")
    op.drop_index("ix_email_token_token_hash", table_name="email_token")
    op.drop_index("ix_email_token_purpose", table_name="email_token")
    op.drop_index("ix_email_token_user_id", table_name="email_token")
    op.drop_index("ix_email_token_tenant_id", table_name="email_token")
    op.drop_table("email_token")

    op.drop_column("user", "email_verified_at")
