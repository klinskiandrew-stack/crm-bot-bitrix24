"""SQLite storage for collected lead reports (table `lead_reports`).

Lives in the shared bot.sqlite — schema is created by db/migrations.sql.
Dedup is by message_id (UNIQUE): re-reading the same chat message never
inserts a duplicate row.
"""

from typing import Any, Dict, List, Optional

import structlog

from db.connection import db

logger = structlog.get_logger()

# Parsed fields written on insert (everything report_parser produces).
_REPORT_FIELDS = (
    "call_datetime", "inn", "company", "phone", "position", "fio",
    "lpr_phone", "email", "city", "comment", "recording_url",
)


async def report_exists(message_id: int) -> bool:
    """True if this chat message was already stored."""
    row = await db.fetch_one(
        "SELECT 1 FROM lead_reports WHERE message_id = ?", (message_id,)
    )
    return row is not None


async def save_report(parsed: Dict[str, Any], message_id: int, chat_id: int):
    """Insert a parsed report. Returns the new row id if created, or None
    if this message_id was already present (dedup). The id is truthy, so
    callers that use it as a boolean still work."""
    columns = ["message_id", "chat_id", *_REPORT_FIELDS, "status"]
    placeholders = ", ".join("?" for _ in columns)
    values = [message_id, chat_id]
    values += [parsed.get(f) or "" for f in _REPORT_FIELDS]
    values.append("parsed")

    cursor = await db.execute(
        f"INSERT OR IGNORE INTO lead_reports ({', '.join(columns)}) "
        f"VALUES ({placeholders})",
        tuple(values),
    )
    await db.commit()
    if cursor.rowcount > 0:
        logger.info(
            "Lead report saved",
            message_id=message_id,
            company=parsed.get("company"),
            phone=parsed.get("phone"),
        )
        return cursor.lastrowid
    return None


async def set_notify_message_id(lead_id: int, message_id: int) -> None:
    """Remember the id of the bot's progress message for this lead."""
    await db.execute(
        "UPDATE lead_reports SET notify_message_id = ? WHERE id = ?",
        (message_id, lead_id),
    )
    await db.commit()


async def get_recent(limit: int = 10) -> List[Dict[str, Any]]:
    """Most recent reports, newest first — for the /leads command."""
    rows = await db.fetch_all(
        "SELECT * FROM lead_reports ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


async def get_stats() -> Dict[str, Any]:
    """Aggregate counts for the /leads_stats command."""
    total_row = await db.fetch_one("SELECT COUNT(*) AS c FROM lead_reports")
    total = total_row["c"] if total_row else 0

    by_status: Dict[str, int] = {}
    for row in await db.fetch_all(
        "SELECT status, COUNT(*) AS c FROM lead_reports GROUP BY status"
    ):
        by_status[row["status"]] = row["c"]

    last_row = await db.fetch_one(
        "SELECT call_datetime FROM lead_reports ORDER BY id DESC LIMIT 1"
    )
    return {
        "total": total,
        "by_status": by_status,
        "last_call_datetime": last_row["call_datetime"] if last_row else None,
    }


async def get_pending_transcription(limit: int = 50) -> List[Dict[str, Any]]:
    """Reports still awaiting transcription — used by Stage 2."""
    rows = await db.fetch_all(
        "SELECT * FROM lead_reports WHERE status = 'parsed' "
        "ORDER BY id ASC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


async def update_transcription(
    lead_id: int, recording_local_path: str, transcript: str
) -> None:
    """Store the downloaded recording path + transcript; status → transcribed."""
    await db.execute(
        "UPDATE lead_reports SET recording_local_path = ?, transcript = ?, "
        "status = 'transcribed', processed_at = CURRENT_TIMESTAMP, error = NULL "
        "WHERE id = ?",
        (recording_local_path, transcript, lead_id),
    )
    await db.commit()


async def mark_error(lead_id: int, error: str) -> None:
    """Flag a report as failed (status='error') with the reason."""
    await db.execute(
        "UPDATE lead_reports SET status = 'error', error = ?, "
        "processed_at = CURRENT_TIMESTAMP WHERE id = ?",
        ((error or "")[:500], lead_id),
    )
    await db.commit()


async def get_by_id(lead_id: int) -> Dict[str, Any]:
    """Fetch one lead row by id (empty dict if missing)."""
    row = await db.fetch_one("SELECT * FROM lead_reports WHERE id = ?", (lead_id,))
    return dict(row) if row else {}


async def get_all_done(limit: int = 5000) -> List[Dict[str, Any]]:
    """Every fully processed lead — used to fully redraw the sheet."""
    rows = await db.fetch_all(
        "SELECT * FROM lead_reports WHERE status = 'done' ORDER BY id ASC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


async def get_pending_analysis(limit: int = 100) -> List[Dict[str, Any]]:
    """Transcribed leads still awaiting AI analysis."""
    rows = await db.fetch_all(
        "SELECT * FROM lead_reports WHERE status = 'transcribed' "
        "ORDER BY id ASC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


async def update_analysis(
    lead_id: int,
    summary: str,
    client_need: str,
    manager_score,
    manager_comment: str,
    lead_temp: str,
) -> None:
    """Store the AI verdict; status → done."""
    await db.execute(
        "UPDATE lead_reports SET ai_summary = ?, ai_client_need = ?, "
        "ai_manager_score = ?, ai_manager_comment = ?, ai_lead_temp = ?, "
        "status = 'done', processed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (summary, client_need, manager_score, manager_comment, lead_temp, lead_id),
    )
    await db.commit()


async def get_exportable(limit: int = 500) -> List[Dict[str, Any]]:
    """Fully processed leads (status='done') not yet in the Google Sheet.

    Only 'done' — so a row reaches the sheet with its AI columns already
    filled, never half-empty.
    """
    rows = await db.fetch_all(
        "SELECT * FROM lead_reports "
        "WHERE status = 'done' AND exported_at IS NULL "
        "ORDER BY id ASC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


async def mark_exported(lead_id: int) -> None:
    """Stamp a lead as exported so it isn't written to the Sheet twice."""
    await db.execute(
        "UPDATE lead_reports SET exported_at = CURRENT_TIMESTAMP WHERE id = ?",
        (lead_id,),
    )
    await db.commit()


# CRM cross-link columns updatable by the enricher (Stage 4).
_CRM_FIELDS = (
    "b24_lead_id", "b24_deal_id", "crm_outcome", "crm_deal_stage",
    "crm_deal_result", "crm_deal_amount", "crm_had_measurement",
    "crm_reason", "crm_manager_comment", "crm_card_url",
)


async def update_crm(lead_id: int, crm: Dict[str, Any]) -> None:
    """Write CRM cross-link data + stamp crm_synced_at. Re-exports the row
    by clearing exported_at so the sheet picks up the fresh values."""
    cols = [f for f in _CRM_FIELDS if f in crm]
    if not cols:
        return
    assignments = ", ".join(f"{c} = ?" for c in cols)
    values = [crm[c] for c in cols]
    values.append(lead_id)
    await db.execute(
        f"UPDATE lead_reports SET {assignments}, "
        f"crm_synced_at = CURRENT_TIMESTAMP, exported_at = NULL WHERE id = ?",
        tuple(values),
    )
    await db.commit()


async def get_for_crm_sync(limit: int = 1000) -> List[Dict[str, Any]]:
    """Leads whose CRM data should be (re)synced: never synced yet, or
    synced but not yet at a FINAL outcome.

    Final = the lead is Неквал, OR a finished deal (Успешна/Провалена) —
    those no longer change. Everything else keeps re-syncing: a lead 'В
    работе' (no deal yet) or 'Не найдено в CRM' may still grow a deal,
    and a live deal moves between stages — so they must not be frozen
    after the first sync.
    """
    rows = await db.fetch_all(
        "SELECT * FROM lead_reports WHERE status = 'done' AND ("
        "  crm_synced_at IS NULL "
        "  OR (crm_outcome != 'Неквал' "
        "      AND (crm_deal_result IS NULL "
        "           OR crm_deal_result NOT IN ('Успешна', 'Провалена')))"
        ") ORDER BY id ASC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]
