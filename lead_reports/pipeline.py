"""Transcription pipeline — turns parsed leads into transcribed ones.

A single global lock serialises all STT work: two Whisper runs at once
would need ~2.5GB and OOM the box. New live reports and manual backfill
both go through transcribe_pending(), so they queue instead of clashing.
The model is unloaded after each batch to free ~1.2GB while idle.
"""

import asyncio
from typing import Any, Dict

import structlog

from lead_reports import audio_processor, lead_db, sheets_exporter, stt

logger = structlog.get_logger()

# Process-wide: only one transcription batch runs at a time.
_stt_lock = asyncio.Lock()


async def transcribe_pending(limit: int = 100) -> Dict[str, Any]:
    """Transcribe every lead still in status='parsed' (up to limit).

    Serialised by _stt_lock — if a batch is already running, this call
    waits, then re-reads the queue (so it picks up anything new too).
    Whisper is unloaded from RAM when the batch finishes.
    """
    async with _stt_lock:
        leads = await lead_db.get_pending_transcription(limit=limit)
        if not leads:
            return {"processed": 0, "ok": 0, "failed": 0}

        logger.info("Transcription batch started", count=len(leads))
        ok = failed = 0
        for lead in leads:
            success = await audio_processor.process_lead(lead)
            if success:
                ok += 1
            else:
                failed += 1

        stt.unload()  # free ~1.2GB until the next batch
        result = {"processed": len(leads), "ok": ok, "failed": failed}
        logger.info("Transcription batch finished", **result)

    # Push the freshly transcribed leads to the Google Sheet. Outside the
    # STT lock — it's just network I/O, no RAM contention.
    try:
        export = await sheets_exporter.export_pending()
        result["exported"] = export.get("exported", 0)
    except Exception as e:
        logger.error("Post-batch sheet export failed", error=str(e))
        result["exported"] = 0
    return result


def trigger_transcription_bg(limit: int = 100) -> None:
    """Fire-and-forget a transcription batch (used by the live listener).

    The lock guarantees no overlap; a redundant call simply finds an
    empty queue and returns. Exceptions are logged, never propagated.
    """
    async def _runner():
        try:
            await transcribe_pending(limit=limit)
        except Exception as e:
            logger.error("Background transcription failed", error=str(e))

    asyncio.create_task(_runner())
