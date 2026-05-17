from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware, types
from aiogram.types import TelegramObject
import json
import structlog
from db.repositories import users as users_repo

logger = structlog.get_logger()


class AuthMiddleware(BaseMiddleware):
    """Middleware for user authentication and context enrichment."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        """Check user authorization and add context."""
        user_id = None

        # Get user_id from message
        if isinstance(event, types.Message):
            user_id = event.from_user.id
        elif isinstance(event, types.CallbackQuery):
            user_id = event.from_user.id
        else:
            return await handler(event, data)

        # Check if user exists in database
        user = await users_repo.get_by_telegram_id(user_id)

        if not user:
            logger.warning("Unknown user attempt", telegram_id=user_id)
            if isinstance(event, types.Message):
                await event.answer("Доступ запрещен. Вы не зарегистрированы в системе.")
            return

        if not user.get("is_active"):
            logger.warning("Inactive user attempt", telegram_id=user_id)
            if isinstance(event, types.Message):
                await event.answer("Ваш аккаунт деактивирован.")
            return

        # Parse b24_user_ids from JSON
        b24_user_ids = []
        if user.get("b24_user_ids"):
            try:
                b24_user_ids = json.loads(user["b24_user_ids"])
            except (json.JSONDecodeError, TypeError):
                logger.error("Invalid b24_user_ids JSON", telegram_id=user_id)

        # allow_private may be missing for old DB rows — default to 1 (allow)
        allow_private = user.get("allow_private")
        if allow_private is None:
            allow_private = 1

        # Add user context to data
        data["user_context"] = {
            "telegram_id": user["telegram_id"],
            "role": user["role"],
            "b24_user_ids": b24_user_ids,
            "display_name": user.get("display_name", f"User{user_id}"),
            "is_admin": user["role"] == "admin",
            "allow_private": bool(allow_private),
        }

        logger.info(
            "User authenticated",
            telegram_id=user_id,
            role=user["role"],
            is_admin=user["role"] == "admin"
        )

        return await handler(event, data)
