#!/usr/bin/env python3
"""One-off backfill of the sphere ИТМ chat history.

Phase 1 — read the whole chat via Telethon, parse every «Онлайн
отчётность» message, store new ones (dedup by message_id).
Phase 2 — transcribe + AI-analyse + export every pending lead.

Run with the live listener DISABLED (LEAD_REPORTS_ENABLED=false) so the
Telethon session and STT don't clash with the bot. Long-running —
launch under nohup:

    nohup venv/bin/python scripts/backfill_lead_reports.py \
        > /tmp/backfill.log 2>&1 &
"""

import asyncio
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from config import settings  # noqa: E402
from db.connection import db  # noqa: E402
from telethon import TelegramClient  # noqa: E402

from lead_reports.lead_db import save_report  # noqa: E402
from lead_reports.report_parser import extract_urls, is_report, parse_report  # noqa: E402
from lead_reports.pipeline import transcribe_pending  # noqa: E402


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def read_history(max_messages: int = 5000):
    """Phase 1 — pull the full chat history, store new reports."""
    client = TelegramClient(
        settings.telethon_session_path,
        settings.telethon_api_id,
        settings.telethon_api_hash,
        connection_retries=30,
        retry_delay=2,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telethon session not authorized")

    entity = await client.get_entity(settings.lead_reports_chat_id)
    reports = saved = 0
    async for msg in client.iter_messages(entity, limit=max_messages):
        if not is_report(msg.message):
            continue
        reports += 1
        parsed = parse_report(msg.message, extract_urls(msg.entities))
        if not parsed:
            continue
        chat_id = getattr(msg, "chat_id", None) or entity.id
        if await save_report(parsed, msg.id, chat_id):
            saved += 1

    await client.disconnect()
    return reports, saved


async def main():
    t0 = time.time()
    await db.init()

    log("Phase 1: reading chat history…")
    reports, saved = await read_history()
    log(f"Phase 1 done: reports found={reports}, new saved={saved}")

    log("Phase 2: transcribe + analyse + export…")
    result = await transcribe_pending(limit=1000)
    log(f"Phase 2 done: {result}")

    await db.close()
    log(f"Backfill finished in {round((time.time() - t0) / 60, 1)} min")


if __name__ == "__main__":
    asyncio.run(main())
