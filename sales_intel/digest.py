"""Weekly 'sales opportunities' digest into the РОП chat.

Runs every detector, formats a prioritised digest (money-at-risk first)
and sends it to sales_digest_chat_id. Wired into the report scheduler.
"""

import html
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode

from config import settings
from sales_intel.detectors import collect_opportunities

logger = structlog.get_logger()

_MSK = timezone(timedelta(hours=3))
_ITEMS_PER_SECTION = 7


def _money(x: float) -> str:
    return f"{int(round(x)):,}".replace(",", " ") + " ₽"


def _esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=False)


def _link(item: Dict[str, Any]) -> str:
    return f'<a href="{html.escape(item["url"], quote=True)}">{_esc(item["title"])}</a>'


def _deal_lines(items: List[Dict[str, Any]], with_amount: bool = True) -> List[str]:
    lines: List[str] = []
    for it in items[:_ITEMS_PER_SECTION]:
        tail: List[str] = []
        if with_amount and it.get("amount"):
            tail.append(_money(it["amount"]))
        if it.get("manager"):
            tail.append(_esc(it["manager"]))
        if it.get("days_idle") is not None:
            tail.append(f'{it["days_idle"]} дн.')
        line = "• " + _link(it)
        if tail:
            line += " — " + " · ".join(tail)
        lines.append(line)
    extra = len(items) - _ITEMS_PER_SECTION
    if extra > 0:
        lines.append(f"…и ещё {extra}")
    return lines


def build_text(data: Dict[str, Any]) -> str:
    """Format the digest message (Telegram HTML)."""
    today = datetime.now(_MSK).strftime("%d.%m.%Y")
    stalled = data.get("measurement_stalled", [])
    stuck = data.get("stuck_deals", [])
    cold = data.get("cold_leads", [])
    fresh = data.get("untouched_leads", [])

    blocks: List[str] = [f"<b>💰 Идеи для продаж — {today}</b>"]

    if not (stalled or stuck or cold or fresh):
        blocks.append("\n✅ Зависших сделок и забытых лидов не найдено — всё в работе.")
        return "\n".join(blocks)

    if stalled:
        risk = sum(i["amount"] for i in stalled)
        blocks.append(
            f"\n<b>🔴 Замеры без продолжения: {len(stalled)}</b>\n"
            "<i>Замер сделан — выезд инженера уже оплачен, но сделка стоит. "
            "Деньги в шаге от закрытия.</i>"
        )
        blocks += _deal_lines(stalled)
        if risk:
            blocks.append(f"Сумма под риском: <b>{_money(risk)}</b>")

    if stuck:
        risk = sum(i["amount"] for i in stuck)
        blocks.append(
            f"\n<b>🟡 Застрявшие сделки: {len(stuck)}</b>\n"
            f"<i>Открыты, но {settings.stuck_deal_days}+ дней без движения по стадии.</i>"
        )
        blocks += _deal_lines(stuck)
        if risk:
            blocks.append(f"Сумма под риском: <b>{_money(risk)}</b>")

    if cold:
        blocks.append(
            f"\n<b>🔵 Забытые лиды: {len(cold)}</b>\n"
            f"<i>Активные лиды без касаний {settings.cold_lead_days}+ дн.</i>"
        )
        blocks += _deal_lines(cold, with_amount=False)

    if fresh:
        lines: List[str] = []
        for it in fresh[:_ITEMS_PER_SECTION]:
            tail = [_esc(it["manager"])] if it.get("manager") else []
            tail.append(f'{it["hours_idle"]} ч')
            lines.append("• " + _link(it) + " — " + " · ".join(tail))
        extra = len(fresh) - _ITEMS_PER_SECTION
        if extra > 0:
            lines.append(f"…и ещё {extra}")
        blocks.append(
            f"\n<b>🆕 Новые лиды без реакции: {len(fresh)}</b>\n"
            f"<i>Созданы, но за {settings.new_lead_react_hours}+ ч никто не взял в работу.</i>"
        )
        blocks += lines

    blocks.append(
        "\n<i>Что делать: красный и жёлтый блоки — обзвонить лично, там уже "
        "вложены силы и деньги. Лиды — раздать менеджерам на сегодня.</i>"
    )
    return "\n".join(blocks)


def _chunks(text: str, limit: int = 3800) -> List[str]:
    """Split into Telegram-sized chunks on line breaks (never mid-tag)."""
    out: List[str] = []
    cur = ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


async def build_digest(assigned_ids: Optional[List[int]] = None) -> str:
    """Run the detectors and return the formatted digest text."""
    data = await collect_opportunities(assigned_ids or [])
    return build_text(data)


async def send_weekly_digest(bot: Bot) -> None:
    """Build the digest and send it to the РОП chat."""
    if not settings.sales_intel_enabled or not settings.sales_digest_chat_id:
        logger.info("Sales digest skipped — disabled or no chat id")
        return
    try:
        text = await build_digest(None)  # None = whole CRM
    except Exception as e:
        logger.error("Sales digest build failed", error=str(e))
        return
    try:
        for chunk in _chunks(text):
            await bot.send_message(
                settings.sales_digest_chat_id,
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        logger.info("Sales digest sent", chat_id=settings.sales_digest_chat_id, chars=len(text))
    except Exception as e:
        logger.error("Sales digest send failed", error=str(e))
