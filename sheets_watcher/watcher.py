"""Мониторинг Google Sheets на новые строки.

Поддерживает несколько таблиц через JSON в settings.sheet_watcher_targets.
По каждой таблице хранит last_seen_count в БД (key='sheet_watcher:{sheet_id}').

Безопасность: используем тот же Service Account что и lead_reports
(secrets/google_sa.json). Таблицу нужно поделить с этим email на чтение.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode

from config import settings
from db.connection import db

logger = structlog.get_logger()


def _format_row(row: List[str], headers: List[str]) -> str:
    """Отформатировать одну строку как короткий HTML для Telegram.
    Берём первые 7 колонок чтобы не разбивать длинными письмами."""
    parts = []
    for i, h in enumerate(headers[:7]):
        if i >= len(row):
            break
        val = (row[i] or "").strip()
        if not val:
            continue
        # экранируем угловые скобки чтобы не сломать HTML
        val = val.replace("<", "&lt;").replace(">", "&gt;")
        h_clean = h.strip().rstrip(":")
        parts.append(f"<b>{h_clean}:</b> {val}")
    return "\n".join(parts)


async def _get_last_seen(sheet_id: str) -> int:
    key = f"sheet_watcher:{sheet_id}"
    row = await db.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    if not row:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


async def _save_last_seen(sheet_id: str, count: int) -> None:
    key = f"sheet_watcher:{sheet_id}"
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, str(count)),
    )
    await db.commit()


def _read_sheet_sync(sheet_id: str, worksheet_index: int = 0) -> List[List[str]]:
    """Blocking gspread call → возвращает все строки (вместе с header)."""
    import gspread
    gc = gspread.service_account(filename="secrets/google_sa.json")
    sh = gc.open_by_key(sheet_id)
    if worksheet_index == 0:
        ws = sh.sheet1
    else:
        ws = sh.get_worksheet(worksheet_index)
    return ws.get_all_values()


async def check_one_sheet(
    bot: Bot,
    sheet_id: str,
    *,
    label: str = "",
    worksheet_index: int = 0,
) -> int:
    """Проверить одну таблицу. Возвращает число новых строк."""
    if not settings.admin_telegram_id:
        logger.info("sheets_watcher skipped — no admin_telegram_id")
        return 0
    try:
        rows = await asyncio.to_thread(_read_sheet_sync, sheet_id, worksheet_index)
    except Exception as e:
        logger.error("sheets_watcher read failed", sheet_id=sheet_id, error=str(e))
        return 0

    total = len(rows)
    if total == 0:
        return 0
    headers = rows[0] if rows else []

    last_seen = await _get_last_seen(sheet_id)
    # Первый запуск — запоминаем текущее состояние без уведомлений
    # (иначе РОПу прилетит все исторические лиды одной кучей).
    if last_seen == 0:
        await _save_last_seen(sheet_id, total)
        logger.info("sheets_watcher initialized", sheet_id=sheet_id, total=total)
        return 0

    if total <= last_seen:
        return 0

    new_rows = rows[last_seen:]
    new_count = len(new_rows)

    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    title = label or "Google Sheets"
    text_parts = [
        f"📋 <b>{title}: новых лидов {new_count}</b>",
        "",
    ]
    for i, row in enumerate(new_rows[:10], 1):
        text_parts.append(f"<b>#{last_seen + i}</b>")
        text_parts.append(_format_row(row, headers))
        text_parts.append("")
    if new_count > 10:
        text_parts.append(f"…и ещё {new_count - 10} строк")
    text_parts.append(f"\n<a href=\"{sheet_url}\">📎 Открыть таблицу</a>")
    text = "\n".join(text_parts)

    try:
        # Telegram parse_mode=HTML лимит 4096; обрезаем если что
        if len(text) > 4000:
            text = text[:3900] + "\n…(обрезано)"
        await bot.send_message(
            settings.admin_telegram_id, text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await _save_last_seen(sheet_id, total)
        logger.info("sheets_watcher notified",
                    sheet_id=sheet_id, new_rows=new_count, total=total)
    except Exception as e:
        logger.error("sheets_watcher notify failed",
                     sheet_id=sheet_id, error=str(e))
    return new_count


async def check_all_watched(bot: Bot) -> Dict[str, int]:
    """Cron entry — пройти по всем настроенным таблицам.

    Конфиг в settings.sheet_watcher_targets — JSON-список
    [{"id": "...", "label": "Авито лиды"}, ...] или один dict.
    """
    raw = (settings.sheet_watcher_targets or "").strip()
    if not raw:
        return {}
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("sheet_watcher_targets must be valid JSON",
                     raw=raw[:200])
        return {}
    if isinstance(targets, dict):
        targets = [targets]
    if not isinstance(targets, list):
        return {}

    out: Dict[str, int] = {}
    for t in targets:
        if not isinstance(t, dict):
            continue
        sid = t.get("id")
        if not sid:
            continue
        label = t.get("label") or ""
        ws_idx = t.get("worksheet_index", 0)
        try:
            ws_idx = int(ws_idx)
        except (TypeError, ValueError):
            ws_idx = 0
        try:
            out[sid] = await check_one_sheet(
                bot, sid, label=label, worksheet_index=ws_idx,
            )
        except Exception as e:
            logger.error("sheets_watcher target failed",
                         sheet_id=sid, error=str(e))
            out[sid] = -1
    return out
