"""Daily self-monitoring digest — what failed in the bot over 24h.

Reads audit_log, groups failed requests by error category and sends a
short factual summary to the admin. No reasoning about causes — that's
the job of the manual «сделай отладку бота» review.
"""

from typing import Dict, List, Tuple

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode

from config import settings
from db.connection import db

logger = structlog.get_logger()


def _categorise(error: str) -> str:
    """Map a raw error string to a human-readable category."""
    e = (error or "").lower()
    if "circuit_breaker" in e:
        return "Сработал предохранитель (лимит токенов/шагов/вызовов)"
    if "max iterations" in e:
        return "Исчерпан лимит итераций"
    if "message text is empty" in e or "message is empty" in e:
        return "Пустой ответ боту"
    if "deepseek" in e or "kie" in e:
        return "Ошибка LLM API"
    if "bitrix" in e:
        return "Ошибка Bitrix24"
    if "timeout" in e or "timed out" in e:
        return "Таймаут"
    return f"Прочее: {(error or '')[:60]}"


async def build_error_digest(hours: int = 24) -> str:
    """Build the digest text for the last `hours` of audit_log."""
    rows = await db.fetch_all(
        "SELECT question, error FROM audit_log "
        "WHERE created_at >= datetime('now', ?)",
        (f"-{hours} hours",),
    )
    total = len(rows)
    failed = [r for r in rows if r["error"]]

    if not failed:
        return (
            f"🔍 <b>Отладка бота</b> — за {hours} ч\n\n"
            f"Запросов: <b>{total}</b>\n"
            f"✅ Ошибок нет."
        )

    # Group by category → list of example questions.
    groups: Dict[str, List[str]] = {}
    for r in failed:
        cat = _categorise(r["error"])
        groups.setdefault(cat, [])
        q = (r["question"] or "").strip().replace("\n", " ")
        if q:
            groups[cat].append(q)

    ordered: List[Tuple[str, List[str]]] = sorted(
        groups.items(), key=lambda kv: len(kv[1]), reverse=True
    )

    pct = round(len(failed) / total * 100, 1) if total else 0
    lines = [
        f"🔍 <b>Отладка бота</b> — за {hours} ч\n",
        f"Запросов: <b>{total}</b>",
        f"Ошибок: <b>{len(failed)}</b> ({pct}%)\n",
        "<b>По типам:</b>",
    ]
    for cat, questions in ordered:
        lines.append(f"\n• {cat} — <b>{len(questions)}</b>")
        for q in questions[:2]:  # up to 2 example questions
            short = q[:80] + ("…" if len(q) > 80 else "")
            lines.append(f"   — «{short}»")

    lines.append(
        "\n\nДля разбора причин и решений — напиши «сделай отладку бота»."
    )
    return "\n".join(lines)


async def send_error_digest(bot: Bot, hours: int = 24) -> None:
    """Send the digest to the admin."""
    if not settings.admin_telegram_id:
        logger.warning("No admin_telegram_id — skipping error digest")
        return
    try:
        text = await build_error_digest(hours=hours)
        await bot.send_message(
            settings.admin_telegram_id, text, parse_mode=ParseMode.HTML
        )
        logger.info("Error digest sent", hours=hours)
    except Exception as e:
        logger.error("Failed to send error digest", error=str(e))
