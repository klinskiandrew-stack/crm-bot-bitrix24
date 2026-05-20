"""Download a call recording and transcribe it (Stage 2).

The lk.ccp.center MP3 links are public — no auth needed. The file is
saved under lead_recordings_dir, transcribed via the local Whisper
wrapper (run in a worker thread so the bot's event loop stays free),
and the lead_reports row is updated.
"""

import asyncio
import os
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

import aiohttp
import structlog

from config import settings
from lead_reports import lead_db, stt

logger = structlog.get_logger()

_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)


def _filename_from_url(url: str) -> str:
    """Last path segment of the URL, falling back to a generic name."""
    name = os.path.basename(urlparse(url).path)
    return name or "recording.mp3"


async def download_recording(url: str, dest_dir: str) -> str:
    """Download the MP3 to dest_dir, return the local file path."""
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    dest = os.path.join(dest_dir, _filename_from_url(url))

    async with aiohttp.ClientSession(timeout=_DOWNLOAD_TIMEOUT) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()

    with open(dest, "wb") as f:
        f.write(data)
    logger.info("Recording downloaded", url=url, dest=dest, size=len(data))
    return dest


async def process_lead(lead: Dict[str, Any]) -> bool:
    """Download + transcribe one lead's recording, update the DB.

    Returns True on success, False on failure (row marked status=error).
    Never raises — the caller processes leads in a loop.
    """
    lead_id = lead.get("id")
    url = lead.get("recording_url")
    if not url:
        await lead_db.mark_error(lead_id, "Нет ссылки на запись разговора")
        return False

    try:
        local_path = await download_recording(url, settings.lead_recordings_dir)
    except Exception as e:
        logger.error("Recording download failed", lead_id=lead_id, error=str(e))
        await lead_db.mark_error(lead_id, f"Скачивание не удалось: {e}")
        return False

    try:
        # Whisper is blocking and CPU-heavy — off the event loop.
        transcript = await asyncio.to_thread(stt.transcribe, local_path)
    except Exception as e:
        logger.error("Transcription failed", lead_id=lead_id, error=str(e))
        await lead_db.mark_error(lead_id, f"Транскрибация не удалась: {e}")
        return False

    if not transcript:
        await lead_db.mark_error(lead_id, "Пустой транскрипт (тишина/короткий звонок?)")
        return False

    await lead_db.update_transcription(lead_id, local_path, transcript)
    logger.info("Lead transcribed", lead_id=lead_id, chars=len(transcript))
    return True
