from aiogram import Router, types, F
from aiogram.enums import ParseMode, ChatAction
from aiogram.types import User
from datetime import date
import asyncio
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

# Bot identity cache (resolved once at first message).
_bot_identity: dict = {"id": None, "username": None}

# Daily global-limit alert state — send admin alert at most once per UTC day.
_daily_alert_sent_on: dict = {"date": None}


# ---------- progress UI helpers ----------

_TOOL_LABELS = {
    "get_deals": "📊 Получаю сделки...",
    "get_deal_details": "🔍 Изучаю детали сделки...",
    "get_leads": "📋 Получаю лиды...",
    "search_contacts_or_companies": "👤 Ищу контакты...",
    "get_pipeline_summary": "📈 Считаю воронку...",
    "get_user_activity_summary": "📅 Собираю активность...",
    "get_recent_activities": "📅 Получаю последние действия...",
    "count_deals_passed_stage": "📊 Считаю по истории стадий...",
}


def _stage_to_text(stage: str, detail: str = "") -> str:
    if stage == "thinking":
        return "🌱 Гроу думает..."
    if stage == "tool":
        return _TOOL_LABELS.get(detail, f"🛠 Выполняю {detail}...")
    if stage == "formatting":
        return "✍️ Формулирую ответ..."
    return "🌱 Гроу думает..."


async def _typing_loop(bot, chat_id):
    """Keep 'typing...' indicator alive until the task is cancelled."""
    try:
        while True:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.debug("Typing loop stopped", error=str(e))


# ---------- alerts ----------

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


# ---------- HTML formatting ----------

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _markdown_to_telegram_html(text: str) -> str:
    """Convert Claude's basic Markdown to Telegram HTML.

    Order matters: escape HTML special chars FIRST (so user data can't
    break parsing), then re-inject our supported tags. Markdown links
    [text](url) are extracted before escaping URL chars.
    """
    links = []

    def _stash(m):
        links.append((m.group(1), m.group(2)))
        return f"\x00LINK{len(links) - 1}\x00"

    text = _MD_LINK_RE.sub(_stash, text)
    safe = html.escape(text, quote=False)
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe, flags=re.DOTALL)
    safe = re.sub(r"__(.+?)__", r"<b>\1</b>", safe, flags=re.DOTALL)
    safe = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", safe)

    def _restore(m):
        idx = int(m.group(1))
        link_text, link_url = links[idx]
        return f'<a href="{html.escape(link_url, quote=True)}">{html.escape(link_text, quote=False)}</a>'

    safe = re.sub(r"\x00LINK(\d+)\x00", _restore, safe)
    return safe


# ---------- trigger detection ----------

async def _ensure_bot_identity(bot):
    """Resolve bot.id and bot.username once, then cache."""
    if _bot_identity["id"] is None:
        me = await bot.me()
        _bot_identity["id"] = me.id
        _bot_identity["username"] = (me.username or "").lower()


def _extract_question(message: types.Message) -> str | None:
    """Return question text if the message addresses the bot, else None.

    Trigger paths:
    - @mention of the bot in the text
    - reply to a message authored by the bot
    """
    if not message.text:
        return None

    bot_username = _bot_identity["username"]
    bot_id = _bot_identity["id"]

    # Path 1: reply to bot's message
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_id
    ):
        return message.text.strip() or None

    # Path 2: @mention of bot
    mention_text = None
    for entity in (message.entities or []):
        if entity.type == "mention":
            candidate = message.text[entity.offset:entity.offset + entity.length]
            if candidate.lower().lstrip("@") == bot_username:
                mention_text = candidate
                break

    if not mention_text:
        return None

    question = message.text.replace(mention_text, "").strip()
    return question or None


# ---------- main handler ----------

@router.message(F.text)
async def handle_mention(message: types.Message, user_context: dict = None):
    """Handle bot mentions and replies in group chats."""

    if not user_context:
        return

    await _ensure_bot_identity(message.bot)
    question = _extract_question(message)
    if question is None:
        return  # not addressed to the bot

    user_id = message.from_user.id
    chat_id = message.chat.id

    # CIRCUIT BREAKER L3 — global daily spend ceiling.
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

    history = await sessions_repo.get_session(user_id, chat_id) or []
    start_time = time.time()

    # Send placeholder so the user immediately sees that the bot took the request.
    placeholder = await message.reply("🌱 Гроу думает...")

    # Start typing indicator loop.
    typing_task = asyncio.create_task(_typing_loop(message.bot, chat_id))

    # Progress callback edits the placeholder as we move through stages.
    # Uses a small debounce to avoid hammering the Telegram edit endpoint.
    last_edit_text = {"v": "🌱 Гроу думает..."}

    async def _progress(stage: str, detail: str = ""):
        new_text = _stage_to_text(stage, detail)
        if new_text == last_edit_text["v"]:
            return
        last_edit_text["v"] = new_text
        try:
            await placeholder.edit_text(new_text)
        except Exception as e:
            # Edit can fail if message identical or rate-limited — non-fatal.
            logger.debug("Progress edit failed", stage=stage, detail=detail, error=str(e))

    try:
        system_prompt = get_system_prompt(
            user_name=user_context["display_name"],
            user_role=user_context["role"],
            assigned_user_ids=user_context["b24_user_ids"],
        )

        response = await orchestrator.process_message(
            question=question,
            user_context=user_context,
            system_prompt=system_prompt,
            history=history,
            progress_callback=_progress,
        )

        answer = response.get("answer", "Ошибка: нет ответа")
        model = response.get("model", "claude-sonnet-4-6")
        tools_called = response.get("tools_called", [])
        duration_ms = response.get("duration_ms", 0)
        error = response.get("error")
        usage = response.get("usage", {})

        await audit_repo.log_request(
            telegram_id=user_id,
            chat_id=chat_id,
            chat_type=message.chat.type,
            question=question,
            model_used=model,
            tools_called=tools_called,
            answer=answer[:1000],
            input_tokens=usage.get("input_tokens", 0),
            cached_input_tokens=usage.get("cache_read_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            credits_consumed=response.get("credits_consumed", 0),
            duration_ms=duration_ms,
            error=error,
        )

        new_history = history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
        ]
        new_history = new_history[-20:]
        await sessions_repo.save_session(user_id, chat_id, new_history)

        # Replace placeholder with final answer. HTML with fallback to plain.
        formatted = _markdown_to_telegram_html(answer)[:4096]
        try:
            await placeholder.edit_text(formatted, parse_mode=ParseMode.HTML)
        except Exception as send_err:
            logger.warning("HTML edit failed, falling back to plain", error=str(send_err))
            try:
                await placeholder.edit_text(answer[:4096])
            except Exception as plain_err:
                logger.warning("Plain edit also failed, sending new message", error=str(plain_err))
                await message.reply(answer[:4096])

        logger.info(
            "Group message processed",
            user_id=user_id,
            chat_id=chat_id,
            duration_ms=duration_ms,
            model=model,
            tools_called=tools_called,
            error=error,
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
            duration_ms=duration_ms,
        )

        try:
            await placeholder.edit_text("⚠️ Ошибка при обработке вопроса. Попробуйте позже.")
        except Exception:
            await message.reply("⚠️ Ошибка при обработке вопроса. Попробуйте позже.")

    finally:
        typing_task.cancel()
