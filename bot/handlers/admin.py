from aiogram import Router, types, F, Bot
from aiogram.filters import Command
import structlog
from config import settings
from db.repositories import audit as audit_repo, settings as settings_repo
from ai.router import router as model_router
from bot.keyboards.admin import get_admin_main_menu

logger = structlog.get_logger()

router = Router()


def is_admin(user_context: dict = None) -> bool:
    """Check if user is admin."""
    if not user_context:
        return False
    return user_context.get("is_admin", False) or user_context["telegram_id"] == settings.admin_telegram_id


@router.message(Command("admin"))
async def admin_command(message: types.Message, user_context: dict = None):
    """Open admin panel."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен. Это команда только для администратора.")
        return

    default_model = await settings_repo.get("default_model") or "claude-sonnet-4-6"
    routing_mode = await settings_repo.get("routing_mode") or "auto"

    response = f"""🔧 АДМИН-ПАНЕЛЬ

Текущая конфигурация:
📌 Модель: {default_model}
🔄 Маршрутизация: {routing_mode}

Выберите действие:
"""

    await message.answer(response, reply_markup=get_admin_main_menu())
    logger.info("Admin panel opened", admin_id=user_context["telegram_id"])


@router.message(Command("set_model"))
async def set_model_command(message: types.Message, user_context: dict = None):
    """Set AI model."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "Использование: /set_model <sonnet|haiku|opus>\n\n"
            "Примеры:\n"
            "/set_model sonnet\n"
            "/set_model haiku\n"
            "/set_model opus"
        )
        return

    model_name = args[1].lower()

    model_mapping = {
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5",
        "opus": "claude-opus-4-7"
    }

    if model_name not in model_mapping:
        await message.answer(f"Неизвестная модель: {model_name}")
        return

    full_model_name = model_mapping[model_name]
    await model_router.set_default_model(full_model_name)
    await settings_repo.set("default_model", full_model_name)

    await message.answer(f"✅ Модель изменена на: {model_name.upper()}")
    logger.info("Model changed", admin_id=user_context["telegram_id"], model=full_model_name)


@router.message(Command("stats"))
async def stats_command(message: types.Message, user_context: dict = None):
    """Show usage statistics."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    try:
        stats = await audit_repo.get_stats(days=7)

        response = "📊 СТАТИСТИКА (последние 7 дней)\n\n"
        response += f"Всего запросов: {stats.get('total_requests', 0)}\n"
        response += f"Ошибок: {stats.get('error_count', 0)}\n"
        response += f"Входных токенов: {stats.get('total_input_tokens', 0)}\n"
        response += f"Выходных токенов: {stats.get('total_output_tokens', 0)}\n"
        response += f"Потрачено credits: {stats.get('total_credits', 0):.2f}\n\n"

        if stats.get('model_distribution'):
            response += "Распределение по моделям:\n"
            for model, count in stats['model_distribution'].items():
                response += f"  {model}: {count}\n"

        await message.answer(response)
        logger.info("Stats shown", admin_id=user_context["telegram_id"])

    except Exception as e:
        logger.error("Error getting stats", error=str(e))
        await message.answer(f"Ошибка при получении статистики: {str(e)}")


@router.message(Command("errors"))
async def errors_command(message: types.Message, user_context: dict = None):
    """Show recent errors."""
    if not is_admin(user_context):
        await message.answer("Доступ запрещен.")
        return

    try:
        errors = await audit_repo.get_recent_errors(limit=5)

        if not errors:
            await message.answer("❌ Нет ошибок в последних записях.")
            return

        response = "❌ ПОСЛЕДНИЕ ОШИБКИ\n\n"
        for err in errors:
            response += f"ID: {err['id']}\n"
            response += f"Пользователь: {err['telegram_id']}\n"
            response += f"Вопрос: {err['question'][:100]}...\n"
            response += f"Ошибка: {err['error'][:100]}...\n"
            response += f"Время: {err['created_at']}\n\n"

        await message.answer(response)
        logger.info("Errors shown", admin_id=user_context["telegram_id"])

    except Exception as e:
        logger.error("Error getting errors", error=str(e))
        await message.answer(f"Ошибка: {str(e)}")
