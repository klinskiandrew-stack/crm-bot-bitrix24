"""Analyse the recorded calls attached to a CRM card (lead or deal).

For each call activity on the card, downloads the MP3 from Bitrix Disk,
transcribes it with the local Whisper, and grades the manager against
the regulation in `lead_reports/manager_call_script.md`.

Transcripts are cached on disk by file_id so re-running the analysis for
the same call doesn't burn Whisper a second time.
"""

import asyncio
import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from b24.client import Bitrix24Client
from lead_reports import call_analyzer, stt

logger = structlog.get_logger()

_CACHE_DIR = Path("/opt/crm-bot/data/call_transcripts")
_MANAGER_SCRIPT = Path(__file__).parent.parent / "lead_reports" / "manager_call_script.md"
_RECORDINGS_DIR = Path("/opt/crm-bot/data/lead_recordings")


def _duration_sec(act: Dict[str, Any]) -> Optional[int]:
    """Activity END_TIME − START_TIME, seconds. None on parse failure."""
    try:
        st = _dt.datetime.fromisoformat(act.get("START_TIME"))
        en = _dt.datetime.fromisoformat(act.get("END_TIME"))
        return int((en - st).total_seconds())
    except Exception:
        return None


def _cached_transcript(file_id: int) -> Optional[str]:
    p = _CACHE_DIR / f"{file_id}.txt"
    if p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


def _save_cache(file_id: int, text: str) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_CACHE_DIR / f"{file_id}.txt").write_text(text, encoding="utf-8")
    except Exception as e:
        logger.warning("Transcript cache save failed", file_id=file_id, error=str(e))


async def _transcribe_recording(
    client: Bitrix24Client, file_id: int
) -> Optional[str]:
    """Download + STT one call file, with on-disk caching by file_id."""
    cached = _cached_transcript(file_id)
    if cached is not None:
        return cached

    data = await client.download_disk_file(file_id)
    if not data:
        return None

    _RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _RECORDINGS_DIR / f"mango_{file_id}.mp3"
    tmp.write_bytes(data)

    async with stt.transcribe_lock:
        try:
            text = await asyncio.to_thread(stt.transcribe, str(tmp))
        finally:
            stt.unload()  # free ~1.2GB on this RAM-tight box

    if text:
        _save_cache(file_id, text)
    return text


async def analyze_card_calls(
    owner_id: int,
    owner_type_id: int = 1,
    limit: int = 10,
    client: Optional[Bitrix24Client] = None,
) -> Dict[str, Any]:
    """Fetch + transcribe + analyse all recorded calls on a lead/deal card.

    owner_type_id: 1=Лид, 2=Сделка.
    Returns a dict with one entry per call (newest first), capped at `limit`.
    """
    own = client is None
    if own:
        client = Bitrix24Client()
        await client._ensure_session()
    try:
        acts = await client.get_call_activities(owner_id, owner_type_id, limit=limit)
        users_map = await client.get_users_map()

        # Only the activities with an attached MP3 file are analysable.
        usable = []
        for a in acts:
            files = a.get("FILES") or []
            if isinstance(files, list) and files:
                a = dict(a)
                a["__file_id"] = files[0].get("id") or files[0].get("ID")
                usable.append(a)

        results = []
        for a in usable:
            file_id = a["__file_id"]
            transcript = await _transcribe_recording(client, file_id)
            entry = {
                "activity_id": a.get("ID"),
                "subject": a.get("SUBJECT"),
                "direction": "исходящий" if str(a.get("DIRECTION")) == "2" else "входящий",
                "started_at": a.get("START_TIME"),
                "duration_sec": _duration_sec(a),
                "manager": _resolve_manager(a.get("RESPONSIBLE_ID"), users_map),
                "transcript_chars": len(transcript or ""),
                "transcript": (transcript or "")[:6000],
            }
            if transcript and len(transcript.strip()) >= 50:
                verdict = await call_analyzer.analyze(
                    transcript,
                    script_path=_MANAGER_SCRIPT,
                    role="менеджер",
                )
                if verdict:
                    entry.update({
                        "summary": verdict.get("summary"),
                        "client_need": verdict.get("client_need"),
                        "manager_score": verdict.get("manager_score"),
                        "manager_comment": verdict.get("manager_comment"),
                        "lead_temp": verdict.get("lead_temp"),
                    })
            results.append(entry)

        return {
            "owner_id": owner_id,
            "owner_type": "lead" if owner_type_id == 1 else "deal",
            "calls_total": len(acts),
            "calls_with_recording": len(usable),
            "calls_analysed": sum(1 for r in results if r.get("manager_score")),
            "calls": results,
        }
    finally:
        if own:
            await client.close()


def _resolve_manager(uid: Any, users_map: Dict[int, Dict[str, str]]) -> str:
    try:
        key = int(uid)
    except (TypeError, ValueError):
        return ""
    info = users_map.get(key)
    return info["name"] if info else (f"#{key}" if key else "")
