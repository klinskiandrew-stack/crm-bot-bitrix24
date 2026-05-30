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
import sys
import structlog

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = structlog.get_logger()


_lead_listener_holder = {"obj": None}


async def _start_lead_listener_background():
    """Запускает Telethon-listener в фоне.

    Telethon connection attempts могут висеть минутами на медленной сети.
    Раньше start_lead_reports_listener() звался синхронно (await) и
    блокировал event loop ДО старта polling — бот в эти 1-3 минуты не
    отвечал. Теперь он крутится отдельной задачей; main продолжается
    немедленно и polling стартует сразу.
    """
    try:
        listener = await start_lead_reports_listener()
        _lead_listener_holder["obj"] = listener
        if listener:
            logger.info("Lead reports listener started in background")
    except Exception as e:
        logger.error("Lead reports listener background start failed", error=str(e))


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

    # Lead reports listener — Telethon watcher на sphere ИТМ.
    # ⚠️ КРИТИЧНО: запускаем в ФОНЕ через asyncio.create_task. Раньше
    # был `await start_lead_reports_listener()` — он висел на Telethon
    # connection retries (4-12 attempts × 12 сек = до 2 минут) и
    # блокировал event loop. До start_polling никогда не доходило, бот
    # молчал часами. Disabled unless LEAD_REPORTS_ENABLED=true.
    lead_listener = None
    lead_listener_task = asyncio.create_task(
        _start_lead_listener_background(),
        name="lead_listener_starter",
    )

    # Polling. aiogram start_polling() при HTTP timeout / сетевой ошибке
    # внутри ловит exception и просто возвращает None — main продолжает
    # жить, бот молчит часами (как было 29.05 — 3 часа без сообщений).
    #
    # Стратегия: ловим CancelledError (graceful shutdown), иначе любой
    # выход из polling считаем аварийным и завершаем процесс с exit(1).
    # systemd (Restart=always, RestartSec=10) поднимет нас через 10 сек.
    # Это надёжнее async-retry потому что освобождает все ресурсы
    # (aiohttp connections, asyncio tasks) и стартует с чистого листа.
    graceful_shutdown = False
    try:
        logger.info("Starting bot polling")
        await dp.start_polling(bot)
        # Если start_polling вышел сам — это аварийный выход (aiogram
        # ловит сетевые ошибки внутри и возвращает None).
        logger.error("Bot polling exited unexpectedly — hard restart via systemd")
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Polling cancelled by signal — graceful shutdown")
        graceful_shutdown = True
    except Exception as e:
        logger.error("Bot polling crashed — hard restart via systemd", error=str(e))

    # ⚠️ АВАРИЙНЫЙ ВЫХОД: НЕ делаем async-cleanup. История 29.05:
    # finally-блок зависал на `await stop_dashboard_server` / Telethon
    # stop / bot.session.close() при мёртвой сети — процесс становился
    # зомби (жив, но polling мёртв), systemd его не рестартил.
    # os._exit(1) убивает процесс НЕМЕДЛЕННО, без finally и без
    # зависающих await. systemd (Restart=always, RestartSec=10) поднимет
    # чистый процесс через 10 секунд.
    if not graceful_shutdown:
        logger.error("Forcing os._exit(1) for clean systemd restart")
        os._exit(1)

    # Graceful shutdown (Ctrl+C / SIGTERM) — спокойно освобождаем ресурсы.
    try:
        if scheduler:
            scheduler.shutdown(wait=False)
        if meetings_scheduler:
            meetings_scheduler.shutdown(wait=False)
        if dashboard_runner and dashboard_scheduler:
            await stop_dashboard_server(dashboard_runner, dashboard_scheduler)
        lead_listener_task.cancel()
        listener_obj = _lead_listener_holder.get("obj")
        if listener_obj:
            try:
                await listener_obj.stop()
            except Exception as e:
                logger.warning("Lead listener stop failed", error=str(e))
        await db.close()
        await bot.session.close()
    except Exception as e:
        logger.warning("Graceful shutdown error", error=str(e))
    logger.info("Bot shutdown", graceful=graceful_shutdown)


if __name__ == "__main__":
    asyncio.run(main())
