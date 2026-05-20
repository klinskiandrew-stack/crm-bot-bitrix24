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


async def save_report(parsed: Dict[str, Any], message_id: int, chat_id: int) -> bool:
    """Insert a parsed report. Returns True if a new row was created,
    False if this message_id was already present (dedup)."""
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
    inserted = cursor.rowcount > 0
    if inserted:
        logger.info(
            "Lead report saved",
            message_id=message_id,
            company=parsed.get("company"),
            phone=parsed.get("phone"),
        )
    return inserted


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
