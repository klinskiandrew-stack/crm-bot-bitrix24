import asyncio
import logging
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from config import settings
from db.connection import db
from bot.dispatcher import create_dispatcher
from bot.utils import get_proxy_config
from reports.scheduler import start_report_scheduler
import structlog

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = structlog.get_logger()


async def main():
    """Main entry point."""
    logger.info("Starting bot", bot_token="***")

    # Initialize database
    await db.init()

    # Create bot with proxy support
    proxy_url = get_proxy_config(settings.telegram_proxy_url)

    if proxy_url:
        session = AiohttpSession(proxy=proxy_url)
        logger.info("Using Telegram proxy", proxy_type=proxy_url.split("://")[0])
    else:
        session = AiohttpSession()
        logger.info("Direct connection to Telegram API")

    bot = Bot(token=settings.telegram_bot_token, session=session)

    # Get bot info
    bot_info = await bot.get_me()
    logger.info("Bot initialized", bot_username=bot_info.username)

    # Create dispatcher and start polling
    dp = create_dispatcher()

    # Scheduled reports (daily/weekly/monthly) — runs in the same event loop
    scheduler = start_report_scheduler(bot)

    try:
        logger.info("Starting bot polling")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error("Bot polling error", error=str(e))
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()
        logger.info("Bot shutdown")


if __name__ == "__main__":
    asyncio.run(main())
