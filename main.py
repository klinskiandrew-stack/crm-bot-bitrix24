import asyncio
import logging
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from config import settings
from db.connection import db
from bot.dispatcher import create_dispatcher
from bot.utils import get_proxy_config
from reports.scheduler import start_report_scheduler
from meetings.scheduler import start_meetings_scheduler
from dashboard.app import start_dashboard_server, stop_dashboard_server
from lead_reports.listener import start_lead_reports_listener
import os
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

    # Meetings module: poll-timeout closer + 30-min reminder dispatcher
    meetings_scheduler = start_meetings_scheduler(bot)

    # Dashboard HTTP server (для VK-специалистов, etc.) — тот же event loop
    dashboard_runner = None
    dashboard_scheduler = None
    if os.getenv("DASHBOARD_ENABLED", "1") != "0":
        try:
            dashboard_host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
            dashboard_port = int(os.getenv("DASHBOARD_PORT", "8001"))
            dashboard_refresh = int(os.getenv("DASHBOARD_REFRESH_MINUTES", "5"))
            dashboard_runner, dashboard_scheduler = await start_dashboard_server(
                host=dashboard_host,
                port=dashboard_port,
                refresh_minutes=dashboard_refresh,
            )
        except Exception as e:
            logger.error("Failed to start dashboard server", error=str(e))

    # Lead reports listener — Telethon watcher on the sphere ИТМ chat.
    # Disabled unless LEAD_REPORTS_ENABLED=true; failure never blocks the bot.
    lead_listener = await start_lead_reports_listener()

    # Polling. aiogram start_polling() при HTTP timeout / сетевой ошибке
    # внутри ловит exception и просто возвращает None — main продолжает
    # жить, бот молчит часами (как было 29.05 — 3 часа без сообщений).
    #
    # Стратегия: ловим CancelledError (graceful shutdown), иначе любой
    # выход из polling считаем аварийным и завершаем процесс с exit(1).
    # systemd (Restart=always, RestartSec=10) поднимет нас через 10 сек.
    # Это надёжнее async-retry потому что освобождает все ресурсы
    # (aiohttp connections, asyncio tasks) и стартует с чистого листа.
    import sys
    graceful_shutdown = False
    try:
        logger.info("Starting bot polling")
        await dp.start_polling(bot)
        # Если start_polling вышел сам — это аварийный выход (aiogram
        # ловит сетевые ошибки внутри и возвращает None).
        logger.error("Bot polling exited unexpectedly — exiting for systemd restart")
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Polling cancelled by signal — graceful shutdown")
        graceful_shutdown = True
    except Exception as e:
        logger.error("Bot polling crashed — exiting for systemd restart", error=str(e))
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        if meetings_scheduler:
            meetings_scheduler.shutdown(wait=False)
        if dashboard_runner and dashboard_scheduler:
            await stop_dashboard_server(dashboard_runner, dashboard_scheduler)
        if lead_listener:
            await lead_listener.stop()
        await db.close()
        await bot.session.close()
        logger.info("Bot shutdown", graceful=graceful_shutdown)

    # ⚠️ Если выход НЕ graceful (signal) — это значит polling упал, надо
    # вернуть systemd ненулевой код чтобы он сделал restart через 10 сек.
    if not graceful_shutdown:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
