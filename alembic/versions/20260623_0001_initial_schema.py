"""Initial database schema.

Revision ID: 20260623_0001
Revises:
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260623_0001"
down_revision: str | None = None
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
    op.create_table(
        "tenant",
        uuid_pk(),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("slug", name="uq_tenant_slug"),
    )

    op.create_table(
        "plan",
        uuid_pk(),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("price_month", sa.Integer(), nullable=False),
        sa.Column("dialog_limit", sa.Integer(), nullable=False),
        sa.Column("channel_limit", sa.Integer(), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.UniqueConstraint("code", name="uq_plan_code"),
    )

    op.create_table(
        "tenant_ai_config",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenant.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("auto_reply_enabled", sa.Boolean(), nullable=False),
        sa.Column("confidence_threshold", sa.Integer(), nullable=False),
        sa.Column("llm_provider", sa.String(length=32), nullable=False),
        sa.Column("embedding_model", sa.String(length=64), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        *timestamps(),
    )

    op.create_table(
        "user",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_user_tenant_id", "user", ["tenant_id"])
    op.create_index("ix_user_email", "user", ["email"], unique=True)

    op.create_table(
        "channel",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_channel_tenant_id", "channel", ["tenant_id"])

    op.create_table(
        "customer",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_customer_tenant_id", "customer", ["tenant_id"])

    op.create_table(
        "kb_document",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("storage_url", sa.String(length=1024), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_kb_document_tenant_id", "kb_document", ["tenant_id"])

    op.create_table(
        "subscription",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column(
            "plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plan.id"), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_subscription_tenant_id", "subscription", ["tenant_id"])

    op.create_table(
        "usage_counter",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("dialogs_count", sa.Integer(), nullable=False),
        sa.Column("ai_replies_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "period", name="uq_usage_counter_tenant_period"),
    )
    op.create_index("ix_usage_counter_tenant_id", "usage_counter", ["tenant_id"])

    op.create_table(
        "refresh_token",
        uuid_pk(),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("user.id"), nullable=False
        ),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
    )
    op.create_index("ix_refresh_token_user_id", "refresh_token", ["user_id"])
    op.create_index("ix_refresh_token_token_hash", "refresh_token", ["token_hash"])

    op.create_table(
        "webhook_event",
        uuid_pk(),
        sa.Column(
            "channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("channel.id"), nullable=False
        ),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "channel_id", "external_event_id", name="uq_webhook_event_channel_external"
        ),
    )
    op.create_index("ix_webhook_event_channel_id", "webhook_event", ["channel_id"])
    op.create_index("ix_webhook_event_external_event_id", "webhook_event", ["external_event_id"])

    op.create_table(
        "customer_identity",
        uuid_pk(),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer.id"),
            nullable=False,
        ),
        sa.Column(
            "channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("channel.id"), nullable=False
        ),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.UniqueConstraint(
            "channel_id", "external_user_id", name="uq_customer_identity_channel_external_user"
        ),
    )
    op.create_index("ix_customer_identity_customer_id", "customer_identity", ["customer_id"])
    op.create_index("ix_customer_identity_channel_id", "customer_identity", ["channel_id"])
    op.create_index(
        "ix_customer_identity_external_user_id", "customer_identity", ["external_user_id"]
    )

    op.create_table(
        "conversation",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer.id"),
            nullable=False,
        ),
        sa.Column(
            "channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("channel.id"), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "assignee_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id"),
            nullable=True,
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_preview", sa.String(length=512), nullable=False),
        sa.Column("unread_count", sa.Integer(), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_conversation_tenant_id", "conversation", ["tenant_id"])

    op.create_table(
        "kb_chunk",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_document.id"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("vector_id", sa.String(length=64), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "document_id", "position", "version", name="uq_kb_chunk_document_position_version"
        ),
    )
    op.create_index("ix_kb_chunk_tenant_id", "kb_chunk", ["tenant_id"])
    op.create_index("ix_kb_chunk_document_id", "kb_chunk", ["document_id"])

    op.create_table(
        "message",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation.id"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("sender_type", sa.String(length=16), nullable=False),
        sa.Column(
            "sender_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("user.id"), nullable=True
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("external_message_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("ai_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "conversation_id", "external_message_id", name="uq_message_conversation_external"
        ),
    )
    op.create_index("ix_message_tenant_id", "message", ["tenant_id"])
    op.create_index("ix_message_conversation_id", "message", ["conversation_id"])
    op.create_index("ix_message_external_message_id", "message", ["external_message_id"])

    op.create_table(
        "kb_candidate",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation.id"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("suggested_by", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "resulting_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_document.id"),
            nullable=True,
        ),
        *timestamps(),
    )
    op.create_index("ix_kb_candidate_tenant_id", "kb_candidate", ["tenant_id"])

    op.create_table(
        "escalation",
        uuid_pk(),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenant.id"), nullable=False
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversation.id"),
            nullable=False,
        ),
        sa.Column(
            "message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("message.id"), nullable=True
        ),
        sa.Column("reason", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "resolved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("user.id"), nullable=True
        ),
        *timestamps(),
    )
    op.create_index("ix_escalation_tenant_id", "escalation", ["tenant_id"])
    op.create_index("ix_escalation_conversation_id", "escalation", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_escalation_conversation_id", table_name="escalation")
    op.drop_index("ix_escalation_tenant_id", table_name="escalation")
    op.drop_table("escalation")

    op.drop_index("ix_kb_candidate_tenant_id", table_name="kb_candidate")
    op.drop_table("kb_candidate")

    op.drop_index("ix_message_external_message_id", table_name="message")
    op.drop_index("ix_message_conversation_id", table_name="message")
    op.drop_index("ix_message_tenant_id", table_name="message")
    op.drop_table("message")

    op.drop_index("ix_kb_chunk_document_id", table_name="kb_chunk")
    op.drop_index("ix_kb_chunk_tenant_id", table_name="kb_chunk")
    op.drop_table("kb_chunk")

    op.drop_index("ix_conversation_tenant_id", table_name="conversation")
    op.drop_table("conversation")

    op.drop_index("ix_customer_identity_external_user_id", table_name="customer_identity")
    op.drop_index("ix_customer_identity_channel_id", table_name="customer_identity")
    op.drop_index("ix_customer_identity_customer_id", table_name="customer_identity")
    op.drop_table("customer_identity")

    op.drop_index("ix_webhook_event_external_event_id", table_name="webhook_event")
    op.drop_index("ix_webhook_event_channel_id", table_name="webhook_event")
    op.drop_table("webhook_event")

    op.drop_index("ix_refresh_token_token_hash", table_name="refresh_token")
    op.drop_index("ix_refresh_token_user_id", table_name="refresh_token")
    op.drop_table("refresh_token")

    op.drop_index("ix_usage_counter_tenant_id", table_name="usage_counter")
    op.drop_table("usage_counter")

    op.drop_index("ix_subscription_tenant_id", table_name="subscription")
    op.drop_table("subscription")

    op.drop_index("ix_kb_document_tenant_id", table_name="kb_document")
    op.drop_table("kb_document")

    op.drop_index("ix_customer_tenant_id", table_name="customer")
    op.drop_table("customer")

    op.drop_index("ix_channel_tenant_id", table_name="channel")
    op.drop_table("channel")

    op.drop_index("ix_user_email", table_name="user")
    op.drop_index("ix_user_tenant_id", table_name="user")
    op.drop_table("user")

    op.drop_table("tenant_ai_config")
    op.drop_table("plan")
    op.drop_table("tenant")
