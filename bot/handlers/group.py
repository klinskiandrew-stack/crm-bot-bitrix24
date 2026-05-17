from aiogram import Router, types, F
from aiogram.types import User
import json
import time
import structlog
from db.repositories import audit as audit_repo, sessions as sessions_repo
from ai.prompts import get_system_prompt
from ai.orchestrator import Orchestrator

logger = structlog.get_logger()

router = Router()
orchestrator = Orchestrator()


@router.message(F.entities)
async def handle_mention(message: types.Message, user_context: dict = None):
    """Handle mentions of the bot in group chats."""

    # Skip if no user context (auth failed)
    if not user_context:
        return

    # Check if message mentions the bot
    mention_text = None

    for entity in message.entities:
        if entity.type == types.MessageEntityType.MENTION:
            mention_text = message.text[entity.offset:entity.offset + entity.length]
            break

    if not mention_text or not message.text:
        return

    # Extract question (remove bot mention)
    question = message.text.replace(mention_text, "").strip()

    if not question:
        await message.reply("Пожалуйста, задайте вопрос.")
        return

    # Load conversation history
    user_id = message.from_user.id
    chat_id = message.chat.id
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

        # Send response
        await message.reply(answer[:4096])  # Telegram limit

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
