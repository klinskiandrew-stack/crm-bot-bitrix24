"""Export transcribed lead calls into the «Лиды Sphere ИТМ» Google Sheet.

One row per call. gspread is synchronous, so the actual sheet writes
run in a worker thread. Dedup is via lead_reports.exported_at — a lead
is written exactly once.
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import gspread
import structlog
from google.oauth2.service_account import Credentials

from config import settings
from lead_reports import lead_db

logger = structlog.get_logger()

# Column order — keep in sync with _lead_to_row().
HEADERS = [
    "Дата и время звонка",
    "Компания",
    "Имя клиента",
    "Телефон",
    "Город",
    "Комментарий",
    "Расшифровка звонка",
    "Ссылка на запись",
    "Статус",
    "Резюме (AI)",
    "Потребность клиента (AI)",
    "Оценка менеджера (AI)",
    "Температура лида (AI)",
]

_STATUS_RU = {
    "parsed": "Без расшифровки",
    "transcribed": "Расшифрован",
    "done": "Обработан",
    "error": "Ошибка",
}

_gc: gspread.Client = None


def is_enabled() -> bool:
    """Export works only with a service-account file and a sheet id."""
    return bool(
        settings.lead_reports_sheet_id
        and settings.google_sa_path
        and Path(settings.google_sa_path).exists()
    )


def _client() -> gspread.Client:
    global _gc
    if _gc is None:
        creds = Credentials.from_service_account_file(
            settings.google_sa_path,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        _gc = gspread.authorize(creds)
    return _gc


def _manager_score_cell(lead: Dict[str, Any]) -> str:
    """Combine the 1-5 score and its comment into one cell."""
    score = lead.get("ai_manager_score")
    comment = lead.get("ai_manager_comment") or ""
    if score is None:
        return comment
    return f"{score}/5 — {comment}".rstrip(" —")


def _lead_to_row(lead: Dict[str, Any]) -> List[str]:
    """Map a lead_reports row to the 13 sheet columns."""
    return [
        lead.get("call_datetime") or "",
        lead.get("company") or "",
        lead.get("fio") or "",
        lead.get("phone") or "",
        lead.get("city") or "",
        lead.get("comment") or "",
        lead.get("transcript") or "",
        lead.get("recording_url") or "",
        _STATUS_RU.get(lead.get("status"), lead.get("status") or ""),
        lead.get("ai_summary") or "",
        lead.get("ai_client_need") or "",
        _manager_score_cell(lead),
        lead.get("ai_lead_temp") or "",
    ]


def _write_rows_sync(rows: List[List[str]]) -> None:
    """Blocking — append rows to the sheet, creating the header first
    time. Runs in a worker thread."""
    sheet = _client().open_by_key(settings.lead_reports_sheet_id)
    ws = sheet.sheet1
    if not ws.acell("A1").value:
        ws.append_row(HEADERS, value_input_option="USER_ENTERED")
    # One API call for the whole batch.
    ws.append_rows(rows, value_input_option="USER_ENTERED")


async def export_pending(limit: int = 500) -> Dict[str, Any]:
    """Push every transcribed-but-not-exported lead to the sheet.

    Returns {"exported": N, "skipped": bool}. Safe to call repeatedly —
    exported_at dedup means nothing is written twice.
    """
    if not is_enabled():
        logger.info("Sheets export disabled (no sheet id / SA file)")
        return {"exported": 0, "skipped": True}

    leads = await lead_db.get_exportable(limit=limit)
    if not leads:
        return {"exported": 0, "skipped": False}

    rows = [_lead_to_row(ld) for ld in leads]
    try:
        await asyncio.to_thread(_write_rows_sync, rows)
    except Exception as e:
        logger.error("Sheets export failed", error=str(e), count=len(rows))
        return {"exported": 0, "skipped": False, "error": str(e)}

    for ld in leads:
        await lead_db.mark_exported(ld["id"])

    logger.info("Leads exported to sheet", count=len(leads))
    return {"exported": len(leads), "skipped": False}
