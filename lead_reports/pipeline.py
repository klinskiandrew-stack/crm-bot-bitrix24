"""Lead processing pipeline: transcribe → AI-analyse → export.

A single global lock serialises STT work: two Whisper runs at once would
need ~2.5GB and OOM the box. AI analysis and the Google Sheet export run
outside that lock — they're network I/O, no RAM contention.

Status flow per lead: parsed → transcribed → done.
"""

import asyncio
from typing import Any, Dict

import structlog

from lead_reports import audio_processor, call_analyzer, lead_db, sheets_exporter, stt

logger = structlog.get_logger()

# Process-wide: only one transcription batch runs at a time.
_stt_lock = asyncio.Lock()


async def _analyse_pending(limit: int = 100) -> int:
    """Run AI analysis on every transcribed-but-not-analysed lead.

    Network-bound (DeepSeek) — safe to run outside the STT lock. A lead
    whose analysis fails stays in status='transcribed' and is retried on
    the next pass. Returns the count successfully analysed.
    """
    leads = await lead_db.get_pending_analysis(limit=limit)
    if not leads:
        return 0

    analysed = 0
    for lead in leads:
        verdict = await call_analyzer.analyze(lead.get("transcript") or "")
        if not verdict:
            continue
        await lead_db.update_analysis(
            lead["id"],
            verdict["summary"],
            verdict["client_need"],
            verdict["manager_score"],
            verdict["manager_comment"],
            verdict["lead_temp"],
        )
        analysed += 1
    logger.info("Analysis batch finished", analysed=analysed, of=len(leads))
    return analysed


async def transcribe_pending(limit: int = 100) -> Dict[str, Any]:
    """Process every pending lead: transcribe → analyse → export.

    Serialised by _stt_lock for the STT part; if a batch is already
    running this call waits, then re-reads the queue. Whisper is
    unloaded from RAM after the transcription phase.
    """
    result: Dict[str, Any] = {"processed": 0, "ok": 0, "failed": 0}

    async with _stt_lock:
        leads = await lead_db.get_pending_transcription(limit=limit)
        if leads:
            logger.info("Transcription batch started", count=len(leads))
            ok = failed = 0
            for lead in leads:
                if await audio_processor.process_lead(lead):
                    ok += 1
                else:
                    failed += 1
            stt.unload()  # free ~1.2GB until the next batch
            result = {"processed": len(leads), "ok": ok, "failed": failed}
            logger.info("Transcription batch finished", **result)

    # AI analysis — outside the STT lock (DeepSeek call, no RAM contention).
    try:
        result["analysed"] = await _analyse_pending(limit=limit)
    except Exception as e:
        logger.error("Analysis phase failed", error=str(e))
        result["analysed"] = 0

    # Push freshly processed leads to the Google Sheet.
    try:
        export = await sheets_exporter.export_pending()
        result["exported"] = export.get("exported", 0)
    except Exception as e:
        logger.error("Post-batch sheet export failed", error=str(e))
        result["exported"] = 0

    return result


def trigger_transcription_bg(limit: int = 100) -> None:
    """Fire-and-forget a full processing batch (used by the live listener).

    The lock guarantees no STT overlap; a redundant call simply finds an
    empty queue and returns. Exceptions are logged, never propagated.
    """
    async def _runner():
        try:
            await transcribe_pending(limit=limit)
        except Exception as e:
            logger.error("Background processing failed", error=str(e))

    asyncio.create_task(_runner())
