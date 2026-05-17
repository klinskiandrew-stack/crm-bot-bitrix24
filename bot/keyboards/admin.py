from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def get_admin_main_menu() -> InlineKeyboardMarkup:
    """Get admin main menu keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="⚙️ Модель", callback_data="admin_model")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="❌ Ошибки", callback_data="admin_errors")],
        [InlineKeyboardButton(text="💰 Баланс Kie", callback_data="admin_balance")],
    ])


def get_model_selection_menu() -> InlineKeyboardMarkup:
    """Get model selection keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🐰 Haiku", callback_data="model_haiku"),
            InlineKeyboardButton(text="🎨 Sonnet", callback_data="model_sonnet"),
            InlineKeyboardButton(text="🦁 Opus", callback_data="model_opus"),
        ],
        [InlineKeyboardButton(text="🔄 Авто маршрутизация", callback_data="routing_auto")],
        [InlineKeyboardButton(text="← Назад", callback_data="admin_back")],
    ])


def get_stats_period_menu() -> InlineKeyboardMarkup:
    """Get statistics period selection keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Сегодня", callback_data="stats_today"),
            InlineKeyboardButton(text="📆 Неделя", callback_data="stats_week"),
        ],
        [
            InlineKeyboardButton(text="📅 Месяц", callback_data="stats_month"),
            InlineKeyboardButton(text="📊 Всё время", callback_data="stats_all"),
        ],
        [InlineKeyboardButton(text="← Назад", callback_data="admin_back")],
    ])


def get_back_button() -> InlineKeyboardMarkup:
    """Get back button."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="admin_back")],
    ])
