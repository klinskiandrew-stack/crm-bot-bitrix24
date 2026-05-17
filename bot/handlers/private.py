from aiogram import Router, types, F
from aiogram.filters import Command
import structlog
from bot.handlers.group import process_question

logger = structlog.get_logger()

router = Router()


@router.message(Command("start"))
async def start_command(message: types.Message, user_context: dict = None):
    """Handle /start command in private chat."""
    if not user_context:
        await message.answer("Доступ запрещен.")
        return

    if not user_context.get("allow_private", True):
        await message.answer(
            "Я общаюсь только в рабочих чатах. Задавайте вопросы там — "
            "просто упомяните меня через @grouasistant_bot."
        )
        return

    await message.answer(
        f"Привет, {user_context['display_name']}! 👋\n\n"
        f"Я — Гроу, ассистент компании Growzone. Спрашивайте про сделки, лидов, "
        f"замеры, продажи, активность менеджеров — отвечу на основе данных из Bitrix24.\n\n"
        f"Просто напишите вопрос в этом чате, например:\n"
        f"• «Сколько было лидов за сегодня?»\n"
        f"• «Покажи воронку продаж за май»\n"
        f"• «Найди контакты с именем Иван»\n\n"
        f"В групповых чатах меня нужно упоминать через @ или ответить на моё сообщение."
    )

    logger.info("User started private chat", user_id=user_context["telegram_id"])


@router.message(F.text, F.chat.type == "private")
async def text_message(message: types.Message, user_context: dict = None):
    """Any text in DM = a question for the bot. Auth is enforced by middleware
    (unknown users get a 'Доступ запрещен' before reaching this handler)."""
    if not user_context:
        return

    if not user_context.get("allow_private", True):
        await message.reply(
            "Я общаюсь только в рабочих чатах. Задавайте вопросы там — "
            "просто упомяните меня через @grouasistant_bot."
        )
        return

    question = (message.text or "").strip()
    if not question:
        return

    await process_question(message, question, user_context)
