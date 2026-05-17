from aiogram import Router, types, F
from aiogram.enums import ParseMode
from aiogram.types import User
from datetime import date
import html
import json
import re
import time
import structlog
from config import settings
from db.repositories import audit as audit_repo, sessions as sessions_repo
from ai.prompts import get_system_prompt
from ai.orchestrator import Orchestrator

logger = structlog.get_logger()

router = Router()
orchestrator = Orchestrator()

# Daily global-limit alert state — send admin alert at most once per UTC day.
_daily_alert_sent_on: dict = {"date": None}


async def _maybe_alert_admin_once(bot, spent: float):
    """Send admin a Telegram alert the first time daily limit hits today."""
    today = date.today()
    if _daily_alert_sent_on["date"] == today:
        return
    _daily_alert_sent_on["date"] = today
    try:
        await bot.send_message(
            settings.admin_telegram_id,
            (
                f"⚠️ <b>Дневной лимит расхода исчерпан</b>\n\n"
                f"Потрачено: <b>{spent:.2f} cr</b>\n"
                f"Лимит: <b>{settings.daily_global_credits_limit:.0f} cr</b>\n\n"
                f"Бот временно НЕ отвечает на запросы до 00:00 UTC."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("Failed to send daily limit alert to admin", error=str(e))


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _markdown_to_telegram_html(text: str) -> str:
    """Convert Claude's basic Markdown to Telegram HTML.

    Order matters: escape HTML special chars FIRST (so user data can't
    break parsing), then re-inject our supported tags. Markdown links
    [text](url) are extracted before escaping URL chars.
    """
    # 1. Extract markdown links to placeholders BEFORE escaping
    links = []

    def _stash(m):
        links.append((m.group(1), m.group(2)))
        return f"\x00LINK{len(links) - 1}\x00"

    text = _MD_LINK_RE.sub(_stash, text)

    # 2. HTML-escape everything (user data, deal titles, etc.)
    safe = html.escape(text, quote=False)

    # 3. Re-inject markdown formatting as HTML tags
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe, flags=re.DOTALL)
    safe = re.sub(r"__(.+?)__", r"<b>\1</b>", safe, flags=re.DOTALL)
    safe = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", safe)

    # 4. Restore links — escape both text (for safety) and url (for quotes)
    def _restore(m):
        idx = int(m.group(1))
        link_text, link_url = links[idx]
        return f'<a href="{html.escape(link_url, quote=True)}">{html.escape(link_text, quote=False)}</a>'

    safe = re.sub(r"\x00LINK(\d+)\x00", _restore, safe)
    return safe


@router.message(F.entities)
async def handle_mention(message: types.Message, user_context: dict = None):
    """Handle mentions of the bot in group chats."""

    # Skip if no user context (auth failed)
    if not user_context:
        return

    # Check if message mentions the bot
    mention_text = None

    for entity in message.entities:
        if entity.type == "mention":
            mention_text = message.text[entity.offset:entity.offset + entity.length]
            break

    if not mention_text or not message.text:
        return

    # Extract question (remove bot mention)
    question = message.text.replace(mention_text, "").strip()

    if not question:
        await message.reply("Пожалуйста, задайте вопрос.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    # CIRCUIT BREAKER L3 — global daily spend ceiling.
    # If the bot has burned through its daily budget, refuse new requests
    # until UTC midnight. Alert admin once.
    daily_spent = await audit_repo.get_daily_credits_spent()
    if daily_spent >= settings.daily_global_credits_limit:
        logger.warning(
            "Daily global limit reached — refusing request",
            user_id=user_id,
            spent=daily_spent,
            limit=settings.daily_global_credits_limit,
        )
        await _maybe_alert_admin_once(message.bot, daily_spent)
        await message.reply(
            "⚠️ Дневной лимит запросов исчерпан. Попробуйте завтра или обратитесь к администратору."
        )
        return

    # Load conversation history
    history = await sessions_repo.get_session(user_id, chat_id) or []

    start_time = time.time()

    try:
        # Build system prompt
        system_prompt = get_system_prompt(
            user_name=user_context["display_name"],
            user_role=user_context["role"],
            assigned_user_ids=user_context["b24_user_ids"]
        )

        # Process through orchestrator with function calling
        response = await orchestrator.process_message(
            question=question,
            user_context=user_context,
            system_prompt=system_prompt,
            history=history
        )

        answer = response.get("answer", "Ошибка: нет ответа")
        model = response.get("model", "claude-sonnet-4-6")
        tools_called = response.get("tools_called", [])
        duration_ms = response.get("duration_ms", 0)
        error = response.get("error")
        usage = response.get("usage", {})

        # Save to audit log
        await audit_repo.log_request(
            telegram_id=user_id,
            chat_id=chat_id,
            chat_type=message.chat.type,
            question=question,
            model_used=model,
            tools_called=tools_called,
            answer=answer[:1000],  # Truncate for storage
            input_tokens=usage.get("input_tokens", 0),
            cached_input_tokens=usage.get("cache_read_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            credits_consumed=response.get("credits_consumed", 0),
            duration_ms=duration_ms,
            error=error
        )

        # Save updated history
        new_history = history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer}
        ]
        # Keep only last N messages
        new_history = new_history[-20:]  # Keep last 20 messages
        await sessions_repo.save_session(user_id, chat_id, new_history)

        # Send response with HTML formatting; fall back to plain text if parser chokes
        formatted = _markdown_to_telegram_html(answer)[:4096]
        try:
            await message.reply(formatted, parse_mode=ParseMode.HTML)
        except Exception as send_err:
            logger.warning("HTML send failed, falling back to plain", error=str(send_err))
            await message.reply(answer[:4096])

        logger.info(
            "Group message processed",
            user_id=user_id,
            chat_id=chat_id,
            duration_ms=duration_ms,
            model=model,
            tools_called=tools_called,
            error=error
        )

    except Exception as e:
        logger.error("Error processing group message", error=str(e), user_id=user_id, chat_id=chat_id)
        duration_ms = int((time.time() - start_time) * 1000)

        await audit_repo.log_request(
            telegram_id=user_id,
            chat_id=chat_id,
            chat_type=message.chat.type,
            question=question,
            model_used="claude-sonnet-4-6",
            error=str(e),
            duration_ms=duration_ms
        )

        await message.reply("Ошибка при обработке вопроса. Пожалуйста, попробуйте позже.")
