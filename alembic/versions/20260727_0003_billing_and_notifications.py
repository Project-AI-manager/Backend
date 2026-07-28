"""Add billing balance and user notification preferences.

Revision ID: 20260727_0003
Revises: 20260704_0002
Create Date: 2026-07-27
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "20260727_0003"
down_revision: str | None = "20260704_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def uuid_type():
    return sa.Uuid(as_uuid=True).with_variant(sa.String(length=32), "sqlite")


def uuid_pk() -> sa.Column:
    return sa.Column("id", uuid_type(), primary_key=True, nullable=False)


def timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.create_table(
        "billing_account",
        uuid_pk(),
        sa.Column("tenant_id", uuid_type(), sa.ForeignKey("tenant.id"), nullable=False),
        sa.Column("balance_kopecks", sa.Integer(), server_default="100000", nullable=False),
        *timestamps(),
        sa.UniqueConstraint("tenant_id", name="uq_billing_account_tenant"),
    )
    op.create_index("ix_billing_account_tenant_id", "billing_account", ["tenant_id"])
    connection = op.get_bind()
    now = datetime.now(UTC)
    tenant_ids = connection.execute(sa.text("SELECT id FROM tenant")).scalars().all()
    if connection.dialect.name == "postgresql":
        identifier = "gen_random_uuid()"
    else:
        identifier = "lower(hex(randomblob(16)))"
    for tenant_id in tenant_ids:
        connection.execute(
            sa.text(
                "INSERT INTO billing_account "
                "(id, tenant_id, balance_kopecks, created_at, updated_at) "
                f"VALUES ({identifier}, :tenant_id, 100000, :created_at, :updated_at)"
            ),
            {"tenant_id": tenant_id, "created_at": now, "updated_at": now},
        )
    op.add_column(
        "usage_counter",
        sa.Column("expenses_kopecks", sa.Integer(), server_default="0", nullable=False),
    )

    op.create_table(
        "user_notification_settings",
        sa.Column(
            "user_id",
            uuid_type(),
            sa.ForeignKey("user.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "escalation_email_enabled", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "daily_digest_email_enabled", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        *timestamps(),
    )


def downgrade() -> None:
    op.drop_table("user_notification_settings")
    op.drop_column("usage_counter", "expenses_kopecks")
    op.drop_index("ix_billing_account_tenant_id", table_name="billing_account")
    op.drop_table("billing_account")
