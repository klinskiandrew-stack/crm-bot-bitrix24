from aiogram import Router, types, F
from aiogram.filters import Command
import structlog

logger = structlog.get_logger()

router = Router()


@router.message(Command("start"))
async def start_command(message: types.Message, user_context: dict = None):
    """Handle /start command in private chat."""
    if not user_context:
        await message.answer("Доступ запрещен.")
        return

    await message.answer(
        f"Привет, {user_context['display_name']}! 👋\n\n"
        f"Я — ассистент для работы с CRM Bitrix24.\n\n"
        f"В группах упоминайте меня, чтобы задать вопрос о сделках, лидах или активности.\n"
        f"Пример: '@bot_name сколько сделок в работе?'\n\n"
        f"Роль: {user_context['role']}"
    )

    logger.info("User started private chat", user_id=user_context["telegram_id"])


@router.message(F.text)
async def text_message(message: types.Message, user_context: dict = None):
    """Handle regular text messages in private chat."""
    if not user_context:
        await message.answer("Доступ запрещен.")
        return

    # For phase 1, private messages are not fully implemented
    await message.answer(
        "На этом этапе свободные вопросы в личных сообщениях еще не поддерживаются. "
        "Используйте упоминания в групповых чатах или выберите команду из меню."
    )
