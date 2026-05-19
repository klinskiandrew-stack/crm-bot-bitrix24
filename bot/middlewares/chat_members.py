"""Middleware that records human chat members for the participant picker.

Runs on every group message. We never store bots. This is best-effort: if
the upsert fails we log and continue — the bot's main job must not depend
on this trace.
"""

from typing import Any, Awaitable, Callable, Dict

import structlog
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from db.repositories import chat_members as chat_members_repo

logger = structlog.get_logger()


class ChatMembersMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            if (
                isinstance(event, Message)
                and event.from_user
                and not event.from_user.is_bot
                and event.chat
                and event.chat.type in ("group", "supergroup")
            ):
                full_name = (
                    (event.from_user.full_name or "").strip()
                    or event.from_user.username
                    or f"User{event.from_user.id}"
                )
                await chat_members_repo.upsert(
                    chat_id=event.chat.id,
                    user_id=event.from_user.id,
                    full_name=full_name,
                    username=event.from_user.username,
                )
        except Exception as e:
            logger.debug("chat_members upsert failed (non-fatal)", error=str(e))

        return await handler(event, data)
