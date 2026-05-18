"""APScheduler — sends daily/weekly/monthly reports to a Telegram chat.

Three cron triggers, all at the same time (e.g. 09:00 Europe/Moscow):
  - daily   — every day, report for YESTERDAY
  - weekly  — Mondays, report for previous Mon-Sun week
  - monthly — 1st of month, report for the previous full month

All three may fire the same morning (e.g. on a Monday that is also the
1st) — that's expected per requirements.

Lifecycle is owned by main.py: start scheduler after Bot is built, stop
on shutdown.
"""

import asyncio
import structlog
from datetime import date, datetime, timedelta
from typing import Optional

from aiogram import Bot
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from b24.client import Bitrix24Client
from config import settings
from reports.builder import build_daily_report, build_period_report

logger = structlog.get_logger()


def _yesterday() -> date:
    return date.today() - timedelta(days=1)


def _previous_week_range() -> tuple[date, date]:
    """Returns (Monday, Sunday) of the week that JUST ended.
    Assumes the job fires on a Monday — gives previous Mon..Sun."""
    today = date.today()
    # last Monday before today (if today=Mon, that's 7 days ago)
    days_since_monday = (today.weekday() + 7) if today.weekday() == 0 else today.weekday()
    last_monday = today - timedelta(days=days_since_monday)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def _previous_month_range() -> tuple[date, date]:
    """First and last day of the previous calendar month."""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - timedelta(days=1)
    first_of_prev = last_of_prev.replace(day=1)
    return first_of_prev, last_of_prev


async def _send(bot: Bot, text: str, label: str) -> None:
    if not settings.reports_chat_id:
        logger.warning("No REPORTS_CHAT_ID configured — skipping send", label=label)
        return
    try:
        # Telegram message hard limit is 4096 chars
        for chunk_start in range(0, len(text), 4000):
            await bot.send_message(
                settings.reports_chat_id,
                text[chunk_start:chunk_start + 4000],
                parse_mode=ParseMode.HTML,
            )
        logger.info("Report sent", label=label, chars=len(text))
    except Exception as e:
        logger.error("Failed to send report", label=label, error=str(e))


async def _send_daily(bot: Bot) -> None:
    day = _yesterday()
    logger.info("Building daily report", day=day.isoformat())
    b24 = Bitrix24Client()
    try:
        text = await build_daily_report(b24, day)
        await _send(bot, text, "daily")
    finally:
        if b24._session and not b24._session.closed:
            await b24._session.close()


async def _send_weekly(bot: Bot) -> None:
    df, dt = _previous_week_range()
    logger.info("Building weekly report", date_from=df.isoformat(), date_to=dt.isoformat())
    b24 = Bitrix24Client()
    try:
        text = await build_period_report(b24, df, dt, title_prefix="Еженедельный отчёт")
        await _send(bot, text, "weekly")
    finally:
        if b24._session and not b24._session.closed:
            await b24._session.close()


async def _send_monthly(bot: Bot) -> None:
    df, dt = _previous_month_range()
    logger.info("Building monthly report", date_from=df.isoformat(), date_to=dt.isoformat())
    b24 = Bitrix24Client()
    try:
        text = await build_period_report(b24, df, dt, title_prefix="Ежемесячный отчёт")
        await _send(bot, text, "monthly")
    finally:
        if b24._session and not b24._session.closed:
            await b24._session.close()


def start_report_scheduler(bot: Bot) -> Optional[AsyncIOScheduler]:
    """Build and start the scheduler. Returns the instance (or None if disabled)."""
    if not settings.reports_enabled or not settings.reports_chat_id:
        logger.info("Reports scheduler disabled (reports_enabled or reports_chat_id empty)")
        return None

    tz = settings.reports_timezone
    hour = settings.reports_daily_hour
    minute = settings.reports_daily_minute

    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        _send_daily,
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        args=[bot],
        id="daily_report",
        misfire_grace_time=3600,  # 1h grace if bot was down
    )
    scheduler.add_job(
        _send_weekly,
        CronTrigger(day_of_week="mon", hour=hour, minute=minute, timezone=tz),
        args=[bot],
        id="weekly_report",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _send_monthly,
        CronTrigger(day=1, hour=hour, minute=minute, timezone=tz),
        args=[bot],
        id="monthly_report",
        misfire_grace_time=3600,
    )

    scheduler.start()
    logger.info(
        "Reports scheduler started",
        chat_id=settings.reports_chat_id,
        timezone=tz,
        daily_at=f"{hour:02d}:{minute:02d}",
    )
    return scheduler
