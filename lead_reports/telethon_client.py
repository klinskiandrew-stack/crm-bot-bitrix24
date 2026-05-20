"""Telethon (User API) client factory for the lead-reports listener.

The bot reads the sphere ИТМ chat as the @teleandrew user account —
Bot API can't see messages from other bots, so a User-API client is
required. A dedicated session file keeps this client from clashing
with the other Telethon processes that share the same account
(barrel-bot, ad-hoc scripts): one .session = one live process.
"""

import structlog
from telethon import TelegramClient

from config import settings

logger = structlog.get_logger()


def build_telethon_client() -> TelegramClient:
    """Construct (not yet connected) the Telethon client.

    connection_retries / retry_delay are tuned for the flaky Timeweb→
    Telegram link — without them the client drops after ~5 attempts.
    """
    return TelegramClient(
        settings.telethon_session_path,  # path WITHOUT the .session suffix
        settings.telethon_api_id,
        settings.telethon_api_hash,
        connection_retries=30,
        retry_delay=2,
    )
