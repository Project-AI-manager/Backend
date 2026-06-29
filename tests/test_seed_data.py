"""Tests for demo seed metadata."""

from app.db.seed import DEMO_CHANNELS, DEMO_CREDENTIALS, build_demo_summary


def test_demo_seed_uses_only_telegram_channel() -> None:
    assert DEMO_CHANNELS == [
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


def test_demo_seed_summary_contains_login_credentials() -> None:
    summary = build_demo_summary()

    assert summary["credentials"] == DEMO_CREDENTIALS
    assert summary["credentials"]["owner_email"] == "owner@demo.ai-manager.local"
    assert summary["credentials"]["password"] == "demo-password"
    assert summary["channels"][0]["type"] == "telegram"
