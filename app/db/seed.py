"""Demo data seeding for local development.

Run after migrations:
    python -m app.db.seed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.channel import Channel
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.models.ops import Escalation, Plan, Subscription, UsageCounter
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import User

DEMO_PASSWORD = "demo-password"
DEMO_OWNER_EMAIL = "owner@demo.ai-manager.local"
DEMO_MANAGER_EMAIL = "manager@demo.ai-manager.local"

DEMO_IDS = {
    "tenant": uuid.UUID("11111111-1111-4111-8111-111111111105"),
    "owner": uuid.UUID("11111111-1111-4111-8111-111111111001"),
    "manager": uuid.UUID("11111111-1111-4111-8111-111111111002"),
    "plan": uuid.UUID("11111111-1111-4111-8111-111111111010"),
    "subscription": uuid.UUID("11111111-1111-4111-8111-111111111011"),
    "usage": uuid.UUID("11111111-1111-4111-8111-111111111012"),
    "telegram_channel": uuid.UUID("11111111-1111-4111-8111-111111111020"),
    "customer_alina": uuid.UUID("11111111-1111-4111-8111-111111111030"),
    "customer_pavel": uuid.UUID("11111111-1111-4111-8111-111111111031"),
    "customer_maria": uuid.UUID("11111111-1111-4111-8111-111111111032"),
    "identity_alina": uuid.UUID("11111111-1111-4111-8111-111111111040"),
    "identity_pavel": uuid.UUID("11111111-1111-4111-8111-111111111041"),
    "identity_maria": uuid.UUID("11111111-1111-4111-8111-111111111042"),
    "conversation_alina": uuid.UUID("11111111-1111-4111-8111-111111111050"),
    "conversation_pavel": uuid.UUID("11111111-1111-4111-8111-111111111051"),
    "conversation_maria": uuid.UUID("11111111-1111-4111-8111-111111111052"),
    "message_1": uuid.UUID("11111111-1111-4111-8111-111111111060"),
    "message_2": uuid.UUID("11111111-1111-4111-8111-111111111061"),
    "message_3": uuid.UUID("11111111-1111-4111-8111-111111111062"),
    "message_4": uuid.UUID("11111111-1111-4111-8111-111111111063"),
    "message_5": uuid.UUID("11111111-1111-4111-8111-111111111064"),
    "message_6": uuid.UUID("11111111-1111-4111-8111-111111111065"),
    "doc_faq": uuid.UUID("11111111-1111-4111-8111-111111111070"),
    "doc_script": uuid.UUID("11111111-1111-4111-8111-111111111071"),
    "chunk_faq_1": uuid.UUID("11111111-1111-4111-8111-111111111080"),
    "chunk_faq_2": uuid.UUID("11111111-1111-4111-8111-111111111081"),
    "chunk_script_1": uuid.UUID("11111111-1111-4111-8111-111111111082"),
    "candidate_1": uuid.UUID("11111111-1111-4111-8111-111111111090"),
    "escalation_1": uuid.UUID("11111111-1111-4111-8111-111111111091"),
}

DEMO_CREDENTIALS = {
    "owner_email": DEMO_OWNER_EMAIL,
    "manager_email": DEMO_MANAGER_EMAIL,
    "password": DEMO_PASSWORD,
}

DEMO_CHANNELS = [
    {
        "type": "telegram",
        "name": "Telegram demo",
        "status": "active",
        "settings": {
            "bot_username": "ai_manager_demo_bot",
            "sync_status": "demo",
            "allowed_updates": ["message"],
        },
    }
]


@dataclass(slots=True)
class SeedStats:
    created: int = 0
    updated: int = 0

    def mark(self, created: bool) -> None:
        if created:
            self.created += 1
        else:
            self.updated += 1


def build_demo_summary() -> dict[str, Any]:
    return {
        "tenant": "ООО Север",
        "credentials": DEMO_CREDENTIALS,
        "channels": DEMO_CHANNELS,
        "customers": ["Алина Петрова", "Павел Смирнов", "Мария Волкова"],
        "knowledge_documents": ["FAQ: доставка и оплата", "Скрипт квалификации лида"],
    }


async def _one_or_none(session: AsyncSession, model: type, **filters: Any) -> Any | None:
    result = await session.execute(select(model).filter_by(**filters))
    return result.scalar_one_or_none()


async def _upsert_by_id(
    session: AsyncSession,
    model: type,
    item_id: uuid.UUID,
    values: dict[str, Any],
) -> tuple[Any, bool]:
    item = await session.get(model, item_id)
    created = item is None
    if created:
        item = model(id=item_id, **values)
        session.add(item)
    else:
        for key, value in values.items():
            setattr(item, key, value)
    return item, created


async def _upsert_tenant(session: AsyncSession, stats: SeedStats) -> Tenant:
    tenant = await _one_or_none(session, Tenant, slug="demo-sever")
    created = tenant is None
    if created:
        tenant = Tenant(id=DEMO_IDS["tenant"])
        session.add(tenant)

    tenant.name = "ООО Север"
    tenant.slug = "demo-sever"
    tenant.status = "active"
    stats.mark(created)
    return tenant


async def _upsert_ai_config(session: AsyncSession, tenant: Tenant, stats: SeedStats) -> None:
    config = await session.get(TenantAIConfig, tenant.id)
    created = config is None
    if created:
        config = TenantAIConfig(tenant_id=tenant.id)
        session.add(config)

    config.auto_reply_enabled = False
    config.confidence_threshold = 80
    config.llm_provider = "mock"
    config.embedding_model = "multilingual-e5-large"
    config.system_prompt = (
        "Ты менеджер по продажам компании ООО Север. Отвечай дружелюбно, "
        "коротко и опирайся только на базу знаний компании."
    )
    stats.mark(created)


async def _upsert_users(
    session: AsyncSession,
    tenant: Tenant,
    stats: SeedStats,
) -> tuple[User, User]:
    password_hash = hash_password(DEMO_PASSWORD)
    users = [
        (
            DEMO_IDS["owner"],
            DEMO_OWNER_EMAIL,
            "Тимур Закиров",
            "owner",
        ),
        (
            DEMO_IDS["manager"],
            DEMO_MANAGER_EMAIL,
            "Анна Менеджер",
            "manager",
        ),
    ]
    saved: list[User] = []
    for user_id, email, full_name, role in users:
        user = await _one_or_none(session, User, email=email)
        created = user is None
        if created:
            user = User(id=user_id)
            session.add(user)

        user.tenant_id = tenant.id
        user.email = email
        user.full_name = full_name
        user.role = role
        user.password_hash = password_hash
        user.status = "active"
        saved.append(user)
        stats.mark(created)

    return saved[0], saved[1]


async def _upsert_plan_and_usage(session: AsyncSession, tenant: Tenant, stats: SeedStats) -> None:
    plan, created = await _upsert_by_id(
        session,
        Plan,
        DEMO_IDS["plan"],
        {
            "code": "demo",
            "name": "Demo",
            "price_month": 0,
            "dialog_limit": 500,
            "channel_limit": 1,
            "features": {"telegram": True, "ml": False, "demo": True},
        },
    )
    stats.mark(created)

    _, created = await _upsert_by_id(
        session,
        Subscription,
        DEMO_IDS["subscription"],
        {"tenant_id": tenant.id, "plan_id": plan.id, "status": "trial"},
    )
    stats.mark(created)

    _, created = await _upsert_by_id(
        session,
        UsageCounter,
        DEMO_IDS["usage"],
        {
            "tenant_id": tenant.id,
            "period": datetime.now(UTC).strftime("%Y-%m"),
            "dialogs_count": 3,
            "ai_replies_count": 1,
        },
    )
    stats.mark(created)


async def _upsert_telegram_channel(
    session: AsyncSession,
    tenant: Tenant,
    stats: SeedStats,
) -> Channel:
    channel_data = DEMO_CHANNELS[0]
    channel, created = await _upsert_by_id(
        session,
        Channel,
        DEMO_IDS["telegram_channel"],
        {
            "tenant_id": tenant.id,
            "type": channel_data["type"],
            "name": channel_data["name"],
            "status": channel_data["status"],
            "credentials_encrypted": "demo-telegram-token-placeholder",
            "settings": channel_data["settings"],
        },
    )
    stats.mark(created)
    return channel


async def _upsert_customers(
    session: AsyncSession,
    tenant: Tenant,
    channel: Channel,
    stats: SeedStats,
) -> tuple[Customer, Customer, Customer]:
    customer_specs = [
        (
            DEMO_IDS["customer_alina"],
            DEMO_IDS["identity_alina"],
            "Алина Петрова",
            "Интересуется внедрением AI-менеджера для отдела продаж.",
            "tg-1001",
        ),
        (
            DEMO_IDS["customer_pavel"],
            DEMO_IDS["identity_pavel"],
            "Павел Смирнов",
            "Уточняет условия оплаты и сроки запуска.",
            "tg-1002",
        ),
        (
            DEMO_IDS["customer_maria"],
            DEMO_IDS["identity_maria"],
            "Мария Волкова",
            "Нужна ручная консультация по интеграции.",
            "tg-1003",
        ),
    ]

    customers: list[Customer] = []
    for customer_id, identity_id, name, note, external_user_id in customer_specs:
        customer, created = await _upsert_by_id(
            session,
            Customer,
            customer_id,
            {"tenant_id": tenant.id, "display_name": name, "note": note},
        )
        stats.mark(created)
        customers.append(customer)

        _, created = await _upsert_by_id(
            session,
            CustomerIdentity,
            identity_id,
            {
                "customer_id": customer.id,
                "channel_id": channel.id,
                "external_user_id": external_user_id,
            },
        )
        stats.mark(created)

    return customers[0], customers[1], customers[2]


async def _upsert_conversations(
    session: AsyncSession,
    tenant: Tenant,
    channel: Channel,
    owner: User,
    manager: User,
    customers: tuple[Customer, Customer, Customer],
    stats: SeedStats,
) -> tuple[Conversation, Conversation, Conversation]:
    now = datetime.now(UTC)
    specs = [
        (
            DEMO_IDS["conversation_alina"],
            customers[0],
            "open",
            manager.id,
            now - timedelta(minutes=8),
            "Хочу понять, сколько времени займёт подключение Telegram.",
            1,
        ),
        (
            DEMO_IDS["conversation_pavel"],
            customers[1],
            "auto",
            None,
            now - timedelta(hours=1),
            "Спасибо, тогда начнём с демо-тарифа.",
            0,
        ),
        (
            DEMO_IDS["conversation_maria"],
            customers[2],
            "escalated",
            owner.id,
            now - timedelta(hours=3),
            "Нужно обсудить нестандартную интеграцию с CRM.",
            2,
        ),
    ]

    conversations: list[Conversation] = []
    for conv_id, customer, status, assignee_id, last_at, preview, unread_count in specs:
        conversation, created = await _upsert_by_id(
            session,
            Conversation,
            conv_id,
            {
                "tenant_id": tenant.id,
                "customer_id": customer.id,
                "channel_id": channel.id,
                "status": status,
                "assignee_user_id": assignee_id,
                "last_message_at": last_at,
                "last_message_preview": preview,
                "unread_count": unread_count,
            },
        )
        stats.mark(created)
        conversations.append(conversation)

    return conversations[0], conversations[1], conversations[2]


async def _upsert_messages(
    session: AsyncSession,
    tenant: Tenant,
    manager: User,
    conversations: tuple[Conversation, Conversation, Conversation],
    stats: SeedStats,
) -> dict[str, Message]:
    message_specs = [
        (
            DEMO_IDS["message_1"],
            conversations[0],
            "inbound",
            "customer",
            None,
            "Здравствуйте! Можно подключить Telegram и проверить ответы до запуска?",
            "tg-msg-1001-1",
            "received",
            None,
        ),
        (
            DEMO_IDS["message_2"],
            conversations[0],
            "outbound",
            "manager",
            manager.id,
            "Да, мы подключим тестовый Telegram-аккаунт и загрузим базу знаний.",
            "tg-msg-1001-2",
            "sent",
            None,
        ),
        (
            DEMO_IDS["message_3"],
            conversations[1],
            "inbound",
            "customer",
            None,
            "Сколько стоит демо и есть ли ограничение по числу диалогов?",
            "tg-msg-1002-1",
            "received",
            None,
        ),
        (
            DEMO_IDS["message_4"],
            conversations[1],
            "outbound",
            "ai",
            None,
            "Демо-тариф бесплатный, включает один Telegram-канал и 500 диалогов.",
            "tg-msg-1002-2",
            "sent",
            0.91,
        ),
        (
            DEMO_IDS["message_5"],
            conversations[2],
            "inbound",
            "customer",
            None,
            "Нам нужна интеграция с CRM и отдельные правила для VIP-клиентов.",
            "tg-msg-1003-1",
            "received",
            None,
        ),
        (
            DEMO_IDS["message_6"],
            conversations[2],
            "outbound",
            "manager",
            manager.id,
            "Передам запрос владельцу проекта, тут лучше обсудить сценарий отдельно.",
            "tg-msg-1003-2",
            "sent",
            None,
        ),
    ]

    messages: dict[str, Message] = {}
    for index, spec in enumerate(message_specs, start=1):
        (
            msg_id,
            conversation,
            direction,
            sender_type,
            sender_id,
            text,
            external_id,
            status,
            confidence,
        ) = spec
        message, created = await _upsert_by_id(
            session,
            Message,
            msg_id,
            {
                "tenant_id": tenant.id,
                "conversation_id": conversation.id,
                "direction": direction,
                "sender_type": sender_type,
                "sender_user_id": sender_id,
                "text": text,
                "attachments": {},
                "external_message_id": external_id,
                "status": status,
                "confidence": confidence,
                "ai_meta": {"source": "demo-seed"} if sender_type == "ai" else {},
            },
        )
        stats.mark(created)
        messages[f"message_{index}"] = message

    return messages


async def _upsert_knowledge_base(
    session: AsyncSession,
    tenant: Tenant,
    conversations: tuple[Conversation, Conversation, Conversation],
    stats: SeedStats,
) -> None:
    doc_faq, created = await _upsert_by_id(
        session,
        KbDocument,
        DEMO_IDS["doc_faq"],
        {
            "tenant_id": tenant.id,
            "title": "FAQ: доставка и оплата",
            "source_type": "manual",
            "storage_url": None,
            "status": "ready",
            "version": 1,
        },
    )
    stats.mark(created)

    doc_script, created = await _upsert_by_id(
        session,
        KbDocument,
        DEMO_IDS["doc_script"],
        {
            "tenant_id": tenant.id,
            "title": "Скрипт квалификации лида",
            "source_type": "manual",
            "storage_url": None,
            "status": "ready",
            "version": 1,
        },
    )
    stats.mark(created)

    chunk_specs = [
        (
            DEMO_IDS["chunk_faq_1"],
            doc_faq,
            "Демо-тариф бесплатный. Он включает один Telegram-канал и 500 диалогов.",
            0,
            {"topic": "billing"},
        ),
        (
            DEMO_IDS["chunk_faq_2"],
            doc_faq,
            "Подключение Telegram в демо-режиме занимает около 15 минут после выдачи токена.",
            1,
            {"topic": "telegram"},
        ),
        (
            DEMO_IDS["chunk_script_1"],
            doc_script,
            "Если клиент просит нестандартную интеграцию, уточни CRM, объём диалогов и сроки.",
            0,
            {"topic": "qualification"},
        ),
    ]
    for chunk_id, document, text, position, tags in chunk_specs:
        _, created = await _upsert_by_id(
            session,
            KbChunk,
            chunk_id,
            {
                "tenant_id": tenant.id,
                "document_id": document.id,
                "text": text,
                "position": position,
                "token_count": len(text.split()),
                "vector_id": f"demo-{chunk_id}",
                "tags": tags,
                "version": document.version,
            },
        )
        stats.mark(created)

    _, created = await _upsert_by_id(
        session,
        KbCandidate,
        DEMO_IDS["candidate_1"],
        {
            "tenant_id": tenant.id,
            "conversation_id": conversations[2].id,
            "question": "Можно ли настроить отдельные правила для VIP-клиентов?",
            "answer": "Да, но это требует отдельной настройки сценария и правил эскалации.",
            "suggested_by": "manager",
            "status": "pending",
            "resulting_document_id": None,
        },
    )
    stats.mark(created)


async def _upsert_escalation(
    session: AsyncSession,
    tenant: Tenant,
    conversations: tuple[Conversation, Conversation, Conversation],
    messages: dict[str, Message],
    stats: SeedStats,
) -> None:
    _, created = await _upsert_by_id(
        session,
        Escalation,
        DEMO_IDS["escalation_1"],
        {
            "tenant_id": tenant.id,
            "conversation_id": conversations[2].id,
            "message_id": messages["message_5"].id,
            "reason": "manual",
            "confidence": 0.42,
            "status": "open",
            "resolved_by": None,
        },
    )
    stats.mark(created)


async def seed_demo_data(session: AsyncSession) -> SeedStats:
    stats = SeedStats()

    tenant = await _upsert_tenant(session, stats)
    await _upsert_ai_config(session, tenant, stats)
    owner, manager = await _upsert_users(session, tenant, stats)
    await _upsert_plan_and_usage(session, tenant, stats)
    channel = await _upsert_telegram_channel(session, tenant, stats)
    customers = await _upsert_customers(session, tenant, channel, stats)
    conversations = await _upsert_conversations(
        session,
        tenant,
        channel,
        owner,
        manager,
        customers,
        stats,
    )
    messages = await _upsert_messages(session, tenant, manager, conversations, stats)
    await _upsert_knowledge_base(session, tenant, conversations, stats)
    await _upsert_escalation(session, tenant, conversations, messages, stats)

    await session.commit()
    return stats


async def _run_seed() -> SeedStats:
    async with SessionLocal() as session:
        return await seed_demo_data(session)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed local demo data.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print demo dataset summary without connecting to the database.",
    )
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(build_demo_summary(), ensure_ascii=False, indent=2))
        return

    stats = asyncio.run(_run_seed())
    print(
        "Demo data seeded: "
        f"created={stats.created}, updated={stats.updated}, "
        f"owner={DEMO_OWNER_EMAIL}, password={DEMO_PASSWORD}"
    )


if __name__ == "__main__":
    main()
