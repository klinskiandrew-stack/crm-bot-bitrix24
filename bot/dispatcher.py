from aiogram import Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
from bot.middlewares.auth import AuthMiddleware
from bot.middlewares.chat_members import ChatMembersMiddleware
from bot.handlers import admin, group, private, callbacks
from bot.handlers import meetings as meetings_handler
import structlog

logger = structlog.get_logger()


def create_dispatcher() -> Dispatcher:
    """Create and configure dispatcher."""
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Track human chat members BEFORE auth so we capture everyone seen
    # in the chat, even users without bot access. Auth still gates handlers.
    dp.message.middleware(ChatMembersMiddleware())

    # Register middleware for authentication
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    # Register routers. meetings_handler routes callbacks for the meetings
    # module — register it before generic callbacks so its filters win first.
    dp.include_router(admin.router)
    dp.include_router(meetings_handler.router)
    dp.include_router(group.router)
    dp.include_router(private.router)
    dp.include_router(callbacks.router)

    logger.info("Dispatcher created and configured")

    return dp
