"""UTF-8 transactional email templates with conservative email-client HTML."""

from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

from app.core.public_urls import public_url_display, public_url_href
from app.services.email_assets import AUTOPILOT_LOGO_CID

BRAND_BLUE = "#2463eb"


def verification_email(*, name: str, code: str, ttl_minutes: int) -> tuple[str, str]:
    display_name = name.strip() or "пользователь"
    text = (
        f"Здравствуйте, {display_name}!\n\n"
        "Ваш код подтверждения для входа в Автопилот:\n\n"
        f"{code}\n\n"
        f"Код действует {ttl_minutes} минут. Никому его не сообщайте.\n\n"
        "Если вы не создавали аккаунт, просто проигнорируйте это письмо."
    )
    html = _layout(
        preview=f"Ваш код подтверждения: {code}",
        title="Подтвердите почту",
        lead=f"Здравствуйте, {escape(display_name)}! Введите этот код в Автопилоте:",
        content=(
            f'<div style="margin:28px 0;text-align:center">'
            f'<span style="display:inline-block;padding:16px 24px;border:1px solid #b9ceff;'
            f'border-radius:12px;background:#eef4ff;color:#1546ad;font-size:32px;'
            f'font-weight:800;letter-spacing:8px;line-height:1;font-family:Arial,sans-serif">'
            f"{escape(code)}</span></div>"
            f'<p style="margin:0;color:#526071;font-size:14px;line-height:22px">'
            f"Код действует {ttl_minutes} минут. Никому его не сообщайте.</p>"
        ),
        footer="Если вы не создавали аккаунт, просто проигнорируйте это письмо.",
    )
    return text, html


def password_reset_email(*, code: str, ttl_minutes: int) -> tuple[str, str]:
    text = (
        "Здравствуйте!\n\n"
        "Ваш код для сброса пароля в Автопилоте:\n\n"
        f"{code}\n\n"
        f"Код действует {ttl_minutes} минут. Никому его не сообщайте.\n\n"
        "Если вы не запрашивали сброс пароля, просто проигнорируйте это письмо."
    )
    html = _layout(
        preview=f"Ваш код для сброса пароля: {code}",
        title="Сброс пароля",
        lead="Введите этот код в Автопилоте, чтобы задать новый пароль:",
        content=(
            '<div style="margin:28px 0;text-align:center">'
            '<span style="display:inline-block;padding:16px 24px;'
            'border:1px solid #b9ceff;border-radius:12px;background:#eef4ff;'
            'color:#1546ad;font-size:32px;font-weight:800;letter-spacing:8px;'
            'line-height:1;font-family:Arial,sans-serif">'
            f"{escape(code)}</span></div>"
            '<p style="margin:0;color:#526071;font-size:14px;line-height:22px">'
            f"Код действует {ttl_minutes} минут. Никому его не сообщайте.</p>"
        ),
        footer="Если вы не запрашивали сброс пароля, просто проигнорируйте это письмо.",
    )
    return text, html


def escalation_email(
    *,
    customer_name: str,
    message_preview: str,
    conversation_url: str,
) -> tuple[str, str]:
    customer = customer_name.strip() or "клиент"
    preview = message_preview.strip()[:1000]
    safe_conversation_href = _safe_url(conversation_url)
    conversation_display_url = (
        public_url_display(safe_conversation_href) if safe_conversation_href else ""
    )
    display_hostname = (
        urlsplit(conversation_display_url).hostname or conversation_display_url
        if conversation_display_url
        else ""
    )
    text = (
        "Автопилоту нужна помощь менеджера.\n\n"
        f"Клиент: {customer}\n"
        f"Последнее сообщение: {preview}\n\n"
        + (f"Открыть диалог: {conversation_display_url}" if conversation_display_url else "")
    )
    html = _layout(
        preview=f"Нужен ответ менеджера — {customer}",
        title="Нужен ответ менеджера",
        lead=(
            f"Клиент <strong>{escape(customer)}</strong> ждёт ответа. "
            "Автопилот передал диалог менеджеру."
        ),
        content=(
            '<div style="margin:24px 0;padding:16px 18px;'
            'border-left:4px solid #e9a52a;border-radius:8px;background:#fff8e8;'
            'color:#344054;font-size:14px;line-height:22px">'
            f"{escape(preview).replace(chr(10), '<br>')}</div>"
            + (
                f'<a href="{escape(safe_conversation_href, quote=True)}" '
                'style="display:inline-block;padding:13px 20px;border-radius:9px;'
                f'background:{BRAND_BLUE};color:#ffffff;font-size:14px;font-weight:700;'
                'text-decoration:none">Открыть диалог на '
                f"{escape(display_hostname)}</a>"
                if safe_conversation_href
                else ""
            )
        ),
        footer="Вы получили письмо, потому что включили уведомления о диалогах, где нужен человек.",
    )
    return text, html


def _safe_url(url: str) -> str:
    """Keep only usable HTTP(S) links and encode IDNs for the href protocol value."""
    normalized = url.strip()
    if not normalized:
        return ""
    try:
        parsed = urlsplit(normalized)
        hostname = (parsed.hostname or "").lower()
        safe_href = public_url_href(normalized)
    except (UnicodeError, ValueError):
        return ""
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return ""
    return safe_href


def _layout(*, preview: str, title: str, lead: str, content: str, footer: str) -> str:
    logo = (
        '<table role="presentation" cellspacing="0" cellpadding="0"><tr>'
        '<td style="width:44px;height:44px;vertical-align:middle">'
        f'<img src="cid:{AUTOPILOT_LOGO_CID}" width="44" height="44" '
        'alt="➤" title="Автопилот" '
        'style="display:block;width:44px;height:44px;border:0;color:#2463eb;'
        'font-size:28px;line-height:44px;text-align:center"></td>'
        '<td style="padding-left:12px;color:#101828;font-size:20px;'
        'font-weight:800">Автопилот</td>'
        "</tr></table>"
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#f4f7fb;font-family:Arial,sans-serif;color:#101828">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{escape(preview)}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f7fb">
<tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px">
<tr><td style="padding:0 4px 18px">{logo}</td></tr>
<tr><td style="padding:32px;border:1px solid #d9e1ec;border-radius:14px;
background:#ffffff;box-shadow:0 12px 32px rgba(18,39,76,.08)">
<h1 style="margin:0 0 14px;font-size:25px;line-height:32px">{escape(title)}</h1>
<p style="margin:0;color:#526071;font-size:15px;line-height:24px">{lead}</p>
{content}
</td></tr>
<tr><td style="padding:18px 8px 0;color:#667085;font-size:12px;line-height:19px;
text-align:center">{escape(footer)}</td></tr>
</table></td></tr></table></body></html>"""
