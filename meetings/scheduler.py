"""APScheduler jobs for the meetings module.

Two periodic jobs (both run every minute):
  1. Reminder dispatch — posts a card 30 min before start.
  2. Poll timeout — finalizes polls whose deadline has passed.
"""

from __future__ import annotations

from typing import Optional

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from db.repositories import (
    meeting_polls as polls_repo,
    scheduled_meetings as meetings_repo,
)
from meetings.flow import format_reminder

logger = structlog.get_logger()


async def _send_reminders(bot: Bot) -> None:
    rows = await meetings_repo.due_for_reminder(settings.meetings_reminder_min_before)
    for row in rows:
        try:
            text = format_reminder(row)
            await bot.send_message(
                row["chat_id"],
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            await meetings_repo.mark_reminder_sent(row["id"])
            logger.info("Meeting reminder sent", meeting_id=row["id"], chat_id=row["chat_id"])
        except Exception as e:
            logger.error("Failed to send meeting reminder", meeting_id=row.get("id"), error=str(e))


async def _close_expired_polls(bot: Bot) -> None:
    rows = await polls_repo.list_expired_open()
    # Local import to avoid circular: handlers depend on flow, scheduler depends on handlers indirectly.
    from bot.handlers.meetings import _finalize_poll
    for poll in rows:
        try:
            logger.info("Closing expired poll", poll_id=poll["id"])
            await _finalize_poll(bot, poll["id"], manual_trigger_message=None)
        except Exception as e:
            logger.error("Failed to close expired poll", poll_id=poll.get("id"), error=str(e))


def start_meetings_scheduler(bot: Bot) -> Optional[AsyncIOScheduler]:
    if not settings.meetings_enabled:
        logger.info("Meetings scheduler disabled (meetings_enabled=false)")
        return None

    scheduler = AsyncIOScheduler(timezone=settings.meetings_timezone)
    scheduler.add_job(
        _send_reminders,
        IntervalTrigger(minutes=1),
        args=[bot],
        id="meetings_reminders",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _close_expired_polls,
        IntervalTrigger(minutes=1),
        args=[bot],
        id="meetings_poll_timeout",
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info(
        "Meetings scheduler started",
        reminder_minutes=settings.meetings_reminder_min_before,
        poll_timeout=settings.meetings_poll_timeout_min,
    )
    return scheduler
