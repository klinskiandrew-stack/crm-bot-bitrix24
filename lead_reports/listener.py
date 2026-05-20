"""Telethon listener — watches the sphere ИТМ chat for new lead reports.

Runs inside the bot's asyncio loop alongside aiogram polling. On each
new message it parses the report and stores it (Stage 1). Transcription
and AI analysis are added in later stages.
"""

import asyncio

import structlog
from telethon import events

from config import settings
from lead_reports.lead_db import save_report
from lead_reports.pipeline import trigger_transcription_bg
from lead_reports.report_parser import extract_urls, is_report, parse_report
from lead_reports.telethon_client import build_telethon_client

logger = structlog.get_logger()

_HEALTHCHECK_INTERVAL_SEC = 300
_HEALTHCHECK_TIMEOUT_SEC = 15


class LeadReportsListener:
    """Owns the Telethon client and the new-message handler."""

    def __init__(self):
        self.client = None
        self._healthcheck_task = None
        self._stopping = False

    async def start(self):
        """Connect, verify auth, register the chat handler + watchdog."""
        self.client = build_telethon_client()
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise RuntimeError(
                "Telethon-сессия не авторизована — запусти scripts/telethon_auth.py"
            )

        me = await self.client.get_me()
        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=[settings.lead_reports_chat_id]),
        )
        self._stopping = False
        self._healthcheck_task = asyncio.create_task(self._healthcheck_loop())
        logger.info(
            "Lead reports listener started",
            account=f"@{me.username}",
            chat_id=settings.lead_reports_chat_id,
        )

    async def _on_new_message(self, event):
        """Parse + store a freshly posted report. Never raises — a bad
        message must not kill the Telethon event loop."""
        msg = event.message
        try:
            text = msg.message or ""
            if not is_report(text):
                return
            parsed = parse_report(text, extract_urls(msg.entities))
            if not parsed:
                return
            saved = await save_report(parsed, msg.id, event.chat_id)
            if saved:
                logger.info(
                    "New lead report collected",
                    message_id=msg.id,
                    company=parsed.get("company"),
                    phone=parsed.get("phone"),
                )
                # Transcribe in the background — the STT lock serialises
                # this with any other running batch, so no RAM clash.
                trigger_transcription_bg()
        except Exception as e:
            logger.error(
                "Failed to process lead report",
                error=str(e),
                message_id=getattr(msg, "id", None),
            )

    async def _healthcheck_loop(self):
        """Telethon auto_reconnect misses 'silent' drops on flaky links —
        ping get_me() periodically and force a reconnect if it hangs."""
        while not self._stopping:
            await asyncio.sleep(_HEALTHCHECK_INTERVAL_SEC)
            if self._stopping:
                break
            try:
                await asyncio.wait_for(
                    self.client.get_me(), timeout=_HEALTHCHECK_TIMEOUT_SEC
                )
            except Exception as e:
                logger.warning("Telethon healthcheck failed, reconnecting", error=str(e))
                try:
                    await self.client.disconnect()
                    await self.client.connect()
                    logger.info("Telethon reconnected")
                except Exception as e2:
                    logger.error("Telethon reconnect failed", error=str(e2))

    async def stop(self):
        """Cancel the watchdog and disconnect the client."""
        self._stopping = True
        if self._healthcheck_task:
            self._healthcheck_task.cancel()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception as e:
                logger.warning("Telethon disconnect error", error=str(e))
        logger.info("Lead reports listener stopped")


async def start_lead_reports_listener():
    """Build + start the listener. Returns the instance, or None if the
    module is disabled or startup failed (bot must keep running either way)."""
    if not settings.lead_reports_enabled:
        logger.info("Lead reports listener disabled (LEAD_REPORTS_ENABLED=false)")
        return None
    listener = LeadReportsListener()
    try:
        await listener.start()
        return listener
    except Exception as e:
        logger.error("Lead reports listener failed to start", error=str(e))
        return None
