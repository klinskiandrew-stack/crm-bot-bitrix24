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
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from b24.client import Bitrix24Client
from config import settings
from reports.builder import build_daily_report, build_period_report

logger = structlog.get_logger()

# Все периоды отчётов в MSK (бизнес-таймзона компании).
# Сервер обычно в UTC, поэтому используем явный MSK всюду где берётся "сегодня/вчера".
_MSK = timezone(timedelta(hours=3))


def _msk_today() -> date:
    return datetime.now(_MSK).date()


def _yesterday() -> date:
    return _msk_today() - timedelta(days=1)


def _previous_week_range() -> tuple[date, date]:
    """Returns (Monday, Sunday) of the week that JUST ended.
    Assumes the job fires on a Monday — gives previous Mon..Sun."""
    today = _msk_today()
    # last Monday before today (if today=Mon, that's 7 days ago)
    days_since_monday = (today.weekday() + 7) if today.weekday() == 0 else today.weekday()
    last_monday = today - timedelta(days=days_since_monday)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def _previous_month_range() -> tuple[date, date]:
    """First and last day of the previous calendar month."""
    today = _msk_today()
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


async def _run_crm_refresh() -> None:
    """Daily lead_reports CRM refresh — re-sync live leads + redraw sheet."""
    try:
        from lead_reports.crm_sync import crm_refresh
        result = await crm_refresh()
        logger.info("CRM refresh job done", **result)
    except Exception as e:
        logger.error("CRM refresh job failed", error=str(e))


async def _run_error_diagnosis(bot: Bot) -> None:
    """Daily AI-powered debug review — audit_log + journalctl → DeepSeek → admin."""
    try:
        from reports.error_digest import send_error_diagnosis
        await send_error_diagnosis(bot, hours=24)
    except Exception as e:
        logger.error("Error diagnosis job failed", error=str(e))


async def _run_lead_retry() -> None:
    """Hourly retry of pending lead recordings — telephony publishes the
    MP3 with a delay, so a fresh call's recording 404s at first."""
    try:
        from lead_reports.pipeline import transcribe_pending
        result = await transcribe_pending(limit=200)
        if result.get("processed"):
            logger.info("Hourly lead retry done", **result)
    except Exception as e:
        logger.error("Hourly lead retry failed", error=str(e))


async def _run_sales_digest(bot: Bot) -> None:
    """Weekly 'sales opportunities' digest — stuck deals + forgotten leads."""
    try:
        from sales_intel.digest import send_weekly_digest
        await send_weekly_digest(bot)
    except Exception as e:
        logger.error("Sales digest job failed", error=str(e))


async def _run_manager_daily(bot: Bot) -> None:
    """Daily manager-activity report → РОП chat at 09:00 MSK."""
    try:
        from reports.manager_daily import send_manager_daily
        await send_manager_daily(bot)
    except Exception as e:
        logger.error("Manager daily report job failed", error=str(e))


async def _run_sales_comms_sync() -> None:
    """Hourly: подтянуть свежие комментарии / activity / OL-сообщения по
    активным сделкам в локальную БД. Берёт сделки которые синкались
    более часа назад, чтобы равномерно крутить ~100 живых сделок."""
    try:
        from b24.client import Bitrix24Client
        from sales_comms.collector import iter_active_deals, sync_deals_bulk
        from sales_comms.db import deals_overdue_for_sync

        client = Bitrix24Client()
        try:
            # Сначала смотрим тех, что вообще ни разу не синкались —
            # фолбэк-режим после деплоя: deal_sync_state пустой, тянем
            # текущий срез активных сделок.
            overdue_ids = await deals_overdue_for_sync(older_than_minutes=60, limit=120)
            if overdue_ids:
                processed, added, calls = await sync_deals_bulk(
                    client, overdue_ids, delay_between=0.3,
                )
                logger.info("sales_comms_sync overdue done",
                            processed=processed, added=added, queued_calls=calls)
            else:
                deals = await iter_active_deals(client, max_items=120)
                ids = [int(d["ID"]) for d in deals if d.get("ID")]
                meta = {int(d["ID"]): d for d in deals if d.get("ID")}
                processed, added, calls = await sync_deals_bulk(
                    client, ids, deals_meta=meta, delay_between=0.3,
                )
                logger.info("sales_comms_sync initial done",
                            processed=processed, added=added, queued_calls=calls)
        finally:
            try:
                await client.close()
            except Exception:
                pass
    except Exception as e:
        logger.error("sales_comms_sync job failed", error=str(e))


async def _run_sales_comms_transcribe() -> None:
    """Every 4 min: жуёт до 5 pending-звонков из deal_communications.
    После batch'а run_batch сам выгружает модель Whisper, чтобы не
    держать 1.2GB постоянно — иначе на 3.8GB сервере OOM-killer."""
    try:
        from sales_comms.transcribe import run_batch
        result = await run_batch(limit=5)
        if result.get("processed"):
            logger.info("sales_comms_transcribe done", **result)
    except Exception as e:
        logger.error("sales_comms_transcribe job failed", error=str(e))


async def _run_sales_comms_progress_notify(bot: Bot) -> None:
    """Hourly: компактная сводка прогресса sales_comms + growth_intel
    в личку админа. Молчит если за час ничего не изменилось."""
    try:
        from sales_comms.notifier import send_hourly_progress
        await send_hourly_progress(bot)
    except Exception as e:
        logger.error("sales_comms progress notify failed", error=str(e))


async def _run_growth_intel_digest(bot: Bot) -> None:
    """Weekly: построить growth_intel дайджест и послать в чат РОПа.

    Долгая задача: refresh_signals прогоняет analyze_deal по всем
    активным сделкам (~60), каждая = DeepSeek-вызов ~3-5 сек. Итого
    5-10 минут. Поэтому только раз в неделю.
    """
    try:
        from growth_intel.digest import build_growth_digest
        from aiogram.enums import ParseMode
        chat_id = settings.sales_digest_chat_id or settings.manager_daily_chat_id
        if not chat_id:
            logger.info("growth_intel digest skipped — no РОП chat configured")
            return
        result = await build_growth_digest(period_days=30, skip_refresh=False)
        text = result["text"]
        for i in range(0, len(text), 4000):
            await bot.send_message(chat_id, text[i:i+4000], parse_mode=ParseMode.HTML)
        logger.info(
            "growth_intel digest sent",
            chat_id=chat_id,
            at_risk=result.get("total_at_risk_rub"),
            signals=result.get("signals_count"),
        )
    except Exception as e:
        logger.error("growth_intel digest job failed", error=str(e))


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
    # lead_reports CRM refresh — daily, a few minutes after the reports so
    # the two don't contend. No-op if lead_reports isn't configured.
    scheduler.add_job(
        _run_crm_refresh,
        CronTrigger(hour=hour, minute=5, timezone=tz),
        id="crm_refresh",
        misfire_grace_time=3600,
    )
    # Daily AI-powered debug review → admin DM at 08:30 MSK.
    # audit_log за сутки + journalctl → DeepSeek → причины + рекомендации.
    scheduler.add_job(
        _run_error_diagnosis,
        CronTrigger(hour=8, minute=30, timezone=tz),
        args=[bot],
        id="error_diagnosis",
        misfire_grace_time=3600,
    )
    # Retry pending lead recordings every 5 minutes. Telephony
    # (lk.ccp.center) publishes the MP3 with a delay — a fresh call 404s
    # on the first attempt — so the bot re-checks the link frequently
    # and picks the recording up within minutes of it appearing, instead
    # of waiting up to an hour. An empty queue makes the run a no-op.
    scheduler.add_job(
        _run_lead_retry,
        CronTrigger(minute="*/5", timezone=tz),
        id="lead_retry",
        misfire_grace_time=240,
    )
    # Daily "manager activity" report → РОП chat at 09:00 MSK.
    if settings.manager_daily_enabled and settings.manager_daily_chat_id:
        scheduler.add_job(
            _run_manager_daily,
            CronTrigger(
                hour=settings.manager_daily_hour,
                minute=settings.manager_daily_minute,
                timezone=tz,
            ),
            args=[bot],
            id="manager_daily",
            misfire_grace_time=3600,
        )

    # sales_comms: hourly sync of comments/activities/OL messages for
    # active deals into local DB. Cheap (≤120 deals × 2 Bitrix calls);
    # feeds the deals_status_digest LLM tool.
    if settings.sales_comms_enabled:
        scheduler.add_job(
            _run_sales_comms_sync,
            CronTrigger(minute=settings.sales_comms_sync_minute, timezone=tz),
            id="sales_comms_sync",
            misfire_grace_time=1800,
        )
        # Whisper для звонков — каждые 4 минуты, до 5 файлов за раз.
        # ⚠️ ИСТОРИЯ: пробовали 8 файлов / 2 мин — словили OOM-killer
        # 9 раз подряд (RSS пиком 3GB при сервере 3.8GB + 2GB swap полон).
        # После batch вызываем stt.unload() — модель Whisper (1.2GB)
        # выгружается, основной бот остаётся ~600MB.
        # Можно полностью отключить флагом если сервер делит память.
        if settings.sales_comms_transcribe_enabled:
            scheduler.add_job(
                _run_sales_comms_transcribe,
                CronTrigger(minute="*/4", timezone=tz),
                id="sales_comms_transcribe",
                misfire_grace_time=180,
            )
        # Часовая сводка прогресса в личку админа (sales_comms + growth_intel).
        # На :45, чтобы данные после sync (:17) и нескольких Whisper-проходов
        # уже были свежие. Молчит если за час ничего не изменилось.
        scheduler.add_job(
            _run_sales_comms_progress_notify,
            CronTrigger(minute=45, timezone=tz),
            args=[bot],
            id="sales_comms_progress_notify",
            misfire_grace_time=600,
        )

    # ⚠️ Прежний еженедельный growth_intel_digest (понедельник 09:15)
    # отключён — теперь growth-блок встроен в ежедневный manager_daily
    # как второе сообщение после блока активности. См. reports/
    # manager_daily.py::send_manager_daily.
    # Сохраняем job как опциональный — на случай если нужно слать
    # отдельный «глубокий» дайджест вручную через /loop или другим
    # каналом. Включить можно флагом growth_intel_weekly_enabled.
    if getattr(settings, "growth_intel_weekly_enabled", False) and (
        settings.sales_digest_chat_id or settings.manager_daily_chat_id
    ):
        scheduler.add_job(
            _run_growth_intel_digest,
            CronTrigger(
                day_of_week=settings.growth_intel_weekday,
                hour=settings.growth_intel_hour,
                minute=settings.growth_intel_minute,
                timezone=tz,
            ),
            args=[bot],
            id="growth_intel_digest",
            misfire_grace_time=3600,
        )

    # Weekly "sales opportunities" digest → РОП chat: stuck deals,
    # measurements without follow-up, forgotten leads.
    if settings.sales_intel_enabled and settings.sales_digest_chat_id:
        scheduler.add_job(
            _run_sales_digest,
            CronTrigger(
                day_of_week=settings.sales_digest_weekday,
                hour=settings.sales_digest_hour,
                minute=settings.sales_digest_minute,
                timezone=tz,
            ),
            args=[bot],
            id="sales_digest",
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
