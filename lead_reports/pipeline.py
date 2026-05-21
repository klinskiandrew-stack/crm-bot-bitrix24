"""Lead processing pipeline: transcribe → AI-analyse → export.

Each lead is carried through the WHOLE path one at a time — transcribe,
analyse, export — so the Google Sheet fills row-by-row and a crash
mid-batch still leaves every finished lead saved and exported.

A single global lock serialises STT work: two Whisper runs at once
would need ~2.5GB and OOM the box.

Status flow per lead: parsed → transcribed → done.
"""

import asyncio
from typing import Any, Dict

import structlog

from b24.client import Bitrix24Client
from lead_reports import (
    audio_processor, call_analyzer, crm_enricher, lead_db, sheets_exporter, stt,
)

logger = structlog.get_logger()

# Whisper is serialised process-wide via stt.transcribe_lock — shared
# with the voice-command handler so the two never run STT concurrently.

# Lazy shared Bitrix client for CRM enrichment.
_b24_client: Bitrix24Client = None


async def _get_b24() -> Bitrix24Client:
    global _b24_client
    if _b24_client is None:
        _b24_client = Bitrix24Client()
        await _b24_client._ensure_session()
    return _b24_client


async def _analyse_and_export(lead_id: int) -> None:
    """Analyse one transcribed lead, enrich it with CRM data, export.

    Network-bound (DeepSeek + Bitrix + Sheets) — no RAM contention. A
    lead whose analysis fails stays 'transcribed' and is retried later.
    """
    from lead_reports.notifications import update_lead_status

    lead = await lead_db.get_by_id(lead_id)
    transcript = lead.get("transcript") if lead else None
    if transcript:
        await update_lead_status(lead, "analyzing")
        verdict = await call_analyzer.analyze(transcript)
        if verdict:
            await lead_db.update_analysis(
                lead_id,
                verdict["summary"],
                verdict["client_need"],
                verdict["manager_score"],
                verdict["manager_comment"],
                verdict["lead_temp"],
            )

    # CRM cross-link: match the call to its Bitrix lead/deal.
    try:
        fresh = await lead_db.get_by_id(lead_id)
        crm = await crm_enricher.enrich(fresh, await _get_b24())
        await lead_db.update_crm(lead_id, crm)
    except Exception as e:
        logger.error("CRM enrichment failed", lead_id=lead_id, error=str(e))

    # Export the row right away so the sheet fills incrementally.
    try:
        await sheets_exporter.export_pending(limit=50)
    except Exception as e:
        logger.error("Incremental export failed", lead_id=lead_id, error=str(e))

    # Final status on the progress message.
    if lead:
        await update_lead_status(lead, "done")


async def transcribe_pending(limit: int = 200) -> Dict[str, Any]:
    """Process every pending lead end-to-end, one at a time.

    Serialised by stt.transcribe_lock. Whisper is unloaded at the end.
    """
    result: Dict[str, Any] = {"processed": 0, "ok": 0, "failed": 0}

    async with stt.transcribe_lock:
        # 1. Catch up leads transcribed earlier but not yet analysed.
        for lead in await lead_db.get_pending_analysis(limit=limit):
            await _analyse_and_export(lead["id"])

        # 2. Transcribe + analyse + export new leads, one by one.
        leads = await lead_db.get_pending_transcription(limit=limit)
        if leads:
            logger.info("Processing batch started", count=len(leads))
            ok = failed = 0
            for lead in leads:
                if await audio_processor.process_lead(lead):
                    ok += 1
                    await _analyse_and_export(lead["id"])
                else:
                    failed += 1
            stt.unload()  # free ~1.2GB until the next batch
            result = {"processed": len(leads), "ok": ok, "failed": failed}
            logger.info("Processing batch finished", **result)

    return result


def trigger_transcription_bg(limit: int = 200) -> None:
    """Fire-and-forget a processing batch (used by the live listener).

    The lock guarantees no STT overlap; a redundant call simply finds an
    empty queue and returns. Exceptions are logged, never propagated.
    """
    async def _runner():
        try:
            await transcribe_pending(limit=limit)
        except Exception as e:
            logger.error("Background processing failed", error=str(e))

    asyncio.create_task(_runner())
