"""Per-call progress notifications in the sphere ИТМ chat.

For every collected report the bot posts ONE message (reply to Amely's
report) and edits it as the call moves through the pipeline:
принят → расшифровка → анализ → готово / ошибка. So the chat shows, per
phone number, exactly what the bot did with it.

Sent via the bot's own token (Bot API) — the bot must be a member of
the chat. Delivery failures are logged, never raised.
"""

from typing import Any, Dict, Optional

import aiohttp
import structlog

from config import settings

logger = structlog.get_logger()

_TIMEOUT = aiohttp.ClientTimeout(total=20)

# Stage → (emoji, text). Drives the edited progress message.
_STAGES = {
    "queued":      ("⏳", "Принят в обработку"),
    "transcribing": ("🎙", "Расшифровка записи…"),
    "waiting":     ("⏳", "Ожидание записи (телефония выкладывает с задержкой)"),
    "analyzing":   ("🤖", "AI-анализ разговора…"),
    "done":        ("✅", "Обработан — расшифрован, проанализирован, в таблице"),
    "error":       ("⚠️", "Ошибка обработки"),
}


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


def _enabled() -> bool:
    return bool(settings.lead_reports_chat_id and settings.telegram_bot_token)


def _status_text(lead: Dict[str, Any], stage: str, detail: str = "") -> str:
    """Build the progress message body for a lead at a given stage."""
    emoji, label = _STAGES.get(stage, ("•", stage))
    head = " · ".join(
        p for p in (lead.get("phone") or "", lead.get("company") or "") if p
    )
    lines = [f"📞 {head}" if head else "📞 звонок", f"{emoji} {label}"]
    if detail:
        lines.append(detail)
    return "\n".join(lines)


async def post_progress(reply_to_message_id: Optional[int], text: str) -> Optional[int]:
    """Post a new progress message (reply to Amely's report if possible).
    Returns the sent message_id, or None on failure."""
    if not _enabled():
        return None
    payload: Dict[str, Any] = {"chat_id": settings.lead_reports_chat_id, "text": text}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(_api_url("sendMessage"), json=payload) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data["result"]["message_id"]
                logger.warning("post_progress failed", body=str(data)[:200])
    except Exception as e:
        logger.warning("post_progress error", error=str(e))
    return None


async def edit_progress(message_id: int, text: str) -> None:
    """Edit an existing progress message."""
    if not _enabled() or not message_id:
        return
    payload = {
        "chat_id": settings.lead_reports_chat_id,
        "message_id": message_id,
        "text": text,
    }
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(_api_url("editMessageText"), json=payload) as resp:
                data = await resp.json()
                # "message is not modified" is harmless — same text re-sent.
                if not data.get("ok") and "not modified" not in str(data):
                    logger.warning("edit_progress failed", body=str(data)[:200])
    except Exception as e:
        logger.warning("edit_progress error", error=str(e))


async def update_lead_status(lead: Dict[str, Any], stage: str, detail: str = "") -> None:
    """Edit the lead's progress message to reflect a new stage."""
    mid = lead.get("notify_message_id")
    if not mid:
        return
    await edit_progress(mid, _status_text(lead, stage, detail))


async def post_lead_queued(
    reply_to_message_id: Optional[int], phone: str, company: str
) -> Optional[int]:
    """Post the initial 'принят в обработку' message for a new report.
    Returns its message_id to be stored as notify_message_id."""
    lead = {"phone": phone, "company": company}
    return await post_progress(reply_to_message_id, _status_text(lead, "queued"))
