"""Notify the sphere ИТМ chat about lead-processing failures.

Sends via the bot's own token (Bot API) — the bot must be a member of
the chat. A delivery failure is logged, never raised: a broken
notification must not break the processing pipeline.
"""

import aiohttp
import structlog

from config import settings

logger = structlog.get_logger()

_TIMEOUT = aiohttp.ClientTimeout(total=20)


async def notify_lead_error(
    company: str = "",
    phone: str = "",
    call_datetime: str = "",
    reason: str = "",
    recording_url: str = "",
) -> None:
    """Post a short failure notice to the lead-reports chat."""
    if not settings.lead_reports_chat_id or not settings.telegram_bot_token:
        return

    parts = ["⚠️ Не удалось обработать звонок"]
    descr = " · ".join(p for p in (call_datetime, company, phone) if p)
    if descr:
        parts.append(descr)
    if reason:
        parts.append(f"Причина: {reason}")
    if recording_url:
        parts.append(recording_url)
    text = "\n".join(parts)

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                url,
                json={"chat_id": settings.lead_reports_chat_id, "text": text},
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    logger.warning(
                        "Lead-error notify failed", status=resp.status, body=body
                    )
    except Exception as e:
        logger.warning("Lead-error notify error", error=str(e))
