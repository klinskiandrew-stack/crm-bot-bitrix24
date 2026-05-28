"""Часовая сводка прогресса sales_comms + growth_intel в личку админа.

Запускается cron'ом каждый час. Считает дельту с прошлого прогона
(snapshot хранится в `settings` key-value таблице), форматирует одно
компактное сообщение и шлёт через bot.send_message.

Содержимое:
  • Расшифровано звонков (всего / +за час)
  • В очереди Whisper (с ETA)
  • Триггеров в БД (всего / горит)
  • Если есть новые горящие триггеры — короткий список.

Не шлёт ничего если за час ничего не изменилось (тихий час → молчим).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode

from config import settings
from db.connection import db

logger = structlog.get_logger()

_SNAPSHOT_KEY = "sales_comms_notifier_snapshot"


async def _snapshot() -> Dict[str, int]:
    """Текущие счётчики из БД."""
    rows = await db.fetch_all(
        """
        SELECT
          (SELECT COUNT(*) FROM deal_sync_state) AS deals_tracked,
          (SELECT COUNT(*) FROM deal_communications WHERE source_type='call') AS calls_total,
          (SELECT COUNT(*) FROM deal_communications WHERE source_type='call' AND transcription_status='done') AS calls_done,
          (SELECT COUNT(*) FROM deal_communications WHERE source_type='call' AND transcription_status='pending') AS calls_pending,
          (SELECT COUNT(*) FROM deal_communications WHERE source_type='call' AND transcription_status='failed') AS calls_failed,
          (SELECT COUNT(*) FROM deal_communications) AS comms_total,
          (SELECT COUNT(*) FROM growth_signals) AS signals_total,
          (SELECT COUNT(*) FROM growth_signals WHERE satisfied=0) AS signals_hot
        """
    )
    if not rows:
        return {}
    r = dict(rows[0])
    return {k: int(v or 0) for k, v in r.items()}


async def _load_prev() -> Optional[Dict[str, int]]:
    row = await db.fetch_one(
        "SELECT value FROM settings WHERE key = ?", (_SNAPSHOT_KEY,)
    )
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


async def _save_snapshot(s: Dict[str, int]) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (_SNAPSHOT_KEY, json.dumps(s)),
    )
    await db.commit()


def _delta(cur: int, prev: Optional[int]) -> str:
    if prev is None:
        return ""
    d = cur - prev
    if d == 0:
        return ""
    return f" (+{d})" if d > 0 else f" ({d})"


def _eta(pending: int, done_per_hour: int) -> str:
    if pending <= 0:
        return "✅ очередь пуста"
    if done_per_hour <= 0:
        return "⏸️ нет движения"
    hours = pending / done_per_hour
    if hours < 1:
        return f"~{int(hours * 60)} мин"
    if hours < 24:
        return f"~{int(hours)} ч"
    return f"~{int(hours / 24)} дн"


async def send_hourly_progress(bot: Bot) -> None:
    """Cron-точка входа. Сравнивает текущий snapshot с прошлым,
    шлёт админу. Никогда не падает (ошибки → лог)."""
    if not settings.admin_telegram_id:
        logger.info("Sales-comms hourly notifier skipped — no admin_telegram_id")
        return
    try:
        cur = await _snapshot()
        if not cur:
            return
        prev = await _load_prev()
        await _save_snapshot(cur)

        # Если изменений нет совсем — молчим (чтобы не спамить ночью).
        # Условие «есть изменения»: что-то добавилось в comms / signals /
        # расшифровках. Сам факт что pending уменьшился — тоже движение.
        if prev:
            zero_delta = all(cur.get(k, 0) == prev.get(k, 0) for k in (
                "comms_total", "calls_done", "signals_total", "signals_hot",
            ))
            if zero_delta:
                logger.info("Sales-comms notifier: no changes since last hour, skip")
                return

        done_delta = cur["calls_done"] - (prev or {}).get("calls_done", 0)
        eta = _eta(cur["calls_pending"], done_delta)

        lines = [
            "<b>🔄 Sales-comms — часовая сводка</b>",
            "",
            f"📊 Сделок отслеживается: <b>{cur['deals_tracked']}</b>"
            + _delta(cur["deals_tracked"], (prev or {}).get("deals_tracked")),
            f"💬 Коммуникаций в базе: <b>{cur['comms_total']}</b>"
            + _delta(cur["comms_total"], (prev or {}).get("comms_total")),
            "",
            "<b>📞 Whisper-расшифровки:</b>",
            f"  расшифровано: {cur['calls_done']} / {cur['calls_total']}"
            + _delta(cur["calls_done"], (prev or {}).get("calls_done")),
            f"  в очереди: {cur['calls_pending']} (ETA {eta})",
        ]
        if cur["calls_failed"]:
            lines.append(f"  ⚠️ failed: {cur['calls_failed']}")

        lines.append("")
        lines.append("<b>🎯 Триггеры роста:</b>")
        lines.append(
            f"  всего найдено: {cur['signals_total']}"
            + _delta(cur["signals_total"], (prev or {}).get("signals_total"))
        )
        hot_delta = _delta(cur["signals_hot"], (prev or {}).get("signals_hot"))
        if cur["signals_hot"]:
            lines.append(f"  🔴 горит: <b>{cur['signals_hot']}</b>{hot_delta}")
        else:
            lines.append(f"  ✅ горящих нет{hot_delta}")

        text = "\n".join(lines)
        await bot.send_message(
            settings.admin_telegram_id, text, parse_mode=ParseMode.HTML
        )
        logger.info("Sales-comms hourly progress sent",
                    chars=len(text), comms_total=cur["comms_total"])
    except Exception as e:
        logger.error("Sales-comms hourly notifier failed", error=str(e))
