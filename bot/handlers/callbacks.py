from aiogram import Router, types, F
import structlog
from config import settings
from db.repositories import audit as audit_repo, settings as settings_repo
from ai.router import router as model_router
from bot.keyboards.admin import (
    get_admin_main_menu,
    get_model_selection_menu,
    get_stats_period_menu,
    get_back_button
)

logger = structlog.get_logger()

router = Router()


def is_admin(user_context: dict = None) -> bool:
    """Check if user is admin."""
    if not user_context:
        return False
    return user_context.get("is_admin", False) or user_context["telegram_id"] == settings.admin_telegram_id


@router.callback_query(F.data == "admin_back")
async def admin_back(query: types.CallbackQuery, user_context: dict = None):
    """Go back to main admin menu."""
    if not is_admin(user_context):
        await query.answer("Доступ запрещен.")
        return

    default_model = await settings_repo.get("default_model") or "claude-sonnet-4-6"
    routing_mode = await settings_repo.get("routing_mode") or "auto"

    response = f"""🔧 АДМИН-ПАНЕЛЬ

Текущая конфигурация:
📌 Модель: {default_model}
🔄 Маршрутизация: {routing_mode}

Выберите действие:
"""

    await query.message.edit_text(response, reply_markup=get_admin_main_menu())
    await query.answer()


@router.callback_query(F.data == "admin_stats")
async def admin_stats(query: types.CallbackQuery, user_context: dict = None):
    """Show statistics menu."""
    if not is_admin(user_context):
        await query.answer("Доступ запрещен.")
        return

    await query.message.edit_text(
        "📊 Выберите период для статистики:",
        reply_markup=get_stats_period_menu()
    )
    await query.answer()


@router.callback_query(F.data.startswith("stats_"))
async def show_stats(query: types.CallbackQuery, user_context: dict = None):
    """Show statistics for selected period."""
    if not is_admin(user_context):
        await query.answer("Доступ запрещен.")
        return

    period_map = {
        "stats_today": (1, "сегодня"),
        "stats_week": (7, "за неделю"),
        "stats_month": (30, "за месяц"),
        "stats_all": (365, "за всё время")
    }

    days, period_name = period_map.get(query.data, (7, "за неделю"))

    try:
        stats = await audit_repo.get_stats(days=days)

        response = f"📊 СТАТИСТИКА {period_name.upper()}\n\n"
        response += f"Всего запросов: {stats.get('total_requests', 0)}\n"
        response += f"Ошибок: {stats.get('error_count', 0)}\n"
        response += f"Входных токенов: {stats.get('total_input_tokens', 0)}\n"
        response += f"Выходных токенов: {stats.get('total_output_tokens', 0)}\n"
        response += f"Среднее входных: {stats.get('avg_input_tokens', 0)}\n"
        response += f"Потрачено credits: {stats.get('total_credits', 0):.2f}\n\n"

        if stats.get('model_distribution'):
            response += "Распределение по моделям:\n"
            for model, count in stats['model_distribution'].items():
                response += f"  {model}: {count}\n"

        await query.message.edit_text(response, reply_markup=get_back_button())
        await query.answer()

    except Exception as e:
        logger.error("Error getting stats", error=str(e))
        await query.answer("Ошибка при получении статистики.")


@router.callback_query(F.data == "admin_model")
async def admin_model(query: types.CallbackQuery, user_context: dict = None):
    """Show model selection menu."""
    if not is_admin(user_context):
        await query.answer("Доступ запрещен.")
        return

    await query.message.edit_text(
        "⚙️ Выберите модель:",
        reply_markup=get_model_selection_menu()
    )
    await query.answer()


@router.callback_query(F.data.startswith("model_"))
async def select_model(query: types.CallbackQuery, user_context: dict = None):
    """Select AI model."""
    if not is_admin(user_context):
        await query.answer("Доступ запрещен.")
        return

    model_map = {
        "model_haiku": "claude-haiku-4-5",
        "model_sonnet": "claude-sonnet-4-6",
        "model_opus": "claude-opus-4-7"
    }

    model_name = model_map.get(query.data)
    if model_name:
        await model_router.set_default_model(model_name)
        await settings_repo.set("default_model", model_name)
        await settings_repo.set("routing_mode", "forced")
        await query.answer(f"✅ Модель установлена: {model_name}")
        logger.info("Model changed", admin_id=user_context["telegram_id"], model=model_name)

        # Go back to main menu
        await admin_back(query, user_context)


@router.callback_query(F.data == "routing_auto")
async def routing_auto(query: types.CallbackQuery, user_context: dict = None):
    """Enable auto routing."""
    if not is_admin(user_context):
        await query.answer("Доступ запрещен.")
        return

    await model_router.set_routing_mode("auto")
    await settings_repo.set("routing_mode", "auto")
    await query.answer("✅ Авто-маршрутизация включена")
    logger.info("Routing mode changed to auto", admin_id=user_context["telegram_id"])

    # Go back to main menu
    await admin_back(query, user_context)


@router.callback_query(F.data == "admin_errors")
async def admin_errors(query: types.CallbackQuery, user_context: dict = None):
    """Show recent errors."""
    if not is_admin(user_context):
        await query.answer("Доступ запрещен.")
        return

    try:
        errors = await audit_repo.get_recent_errors(limit=5)

        if not errors:
            response = "✅ Нет ошибок в последних записях."
        else:
            response = "❌ ПОСЛЕДНИЕ ОШИБКИ\n\n"
            for err in errors:
                response += f"ID: {err['id']}\n"
                response += f"Пользователь: {err['telegram_id']}\n"
                response += f"Вопрос: {err['question'][:50]}...\n"
                response += f"Ошибка: {err['error'][:80]}...\n\n"

        await query.message.edit_text(response, reply_markup=get_back_button())
        await query.answer()

    except Exception as e:
        logger.error("Error getting errors", error=str(e))
        await query.answer("Ошибка при получении ошибок.")


@router.callback_query(F.data == "admin_balance")
async def admin_balance(query: types.CallbackQuery, user_context: dict = None):
    """Show Kie balance (placeholder for phase 3)."""
    if not is_admin(user_context):
        await query.answer("Доступ запрещен.")
        return

    response = "💰 Баланс Kie.ai\n\n"
    response += "Проверка баланса будет добавлена на этапе оптимизации (фаза 3)."

    await query.message.edit_text(response, reply_markup=get_back_button())
    await query.answer()


@router.callback_query(F.data == "admin_users")
async def admin_users(query: types.CallbackQuery, user_context: dict = None):
    """Show users (placeholder for phase 2)."""
    if not is_admin(user_context):
        await query.answer("Доступ запрещен.")
        return

    response = "👥 Управление пользователями\n\n"
    response += "Управление пользователями будет добавлено позже."

    await query.message.edit_text(response, reply_markup=get_back_button())
    await query.answer()
