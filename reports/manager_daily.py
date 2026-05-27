"""Daily 'work-of-managers' report → РОП chat at 09:00 MSK.

Pulls yesterday's activities and new-deal counts for the three sales
managers, formats a compact Telegram-HTML message. Pure stats — no LLM
call needed (cheap, fast, deterministic).
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode

from b24.client import Bitrix24Client
from config import settings

logger = structlog.get_logger()

_MSK = timezone(timedelta(hours=3))

# Sales department roster — see the user-provided list (memory + system
# prompt). РОП Виктория Евстифеева — НЕ менеджер, в отчёт не включается.
SALES_MANAGERS = ("Шеян Андрей", "Ребров Никита", "Останина Любовь")

# Bitrix activity TYPE_ID → русские формы (1 / 2-4 / 5+) для склонения.
_ACTIVITY_FORMS = {
    1: ("встреча", "встречи", "встреч"),
    2: ("звонок",  "звонка",  "звонков"),
    3: ("задача",  "задачи",  "задач"),
    4: ("письмо",  "письма",  "писем"),
    6: ("чат",     "чата",    "чатов"),
}


def _yesterday_msk() -> date:
    return (datetime.now(_MSK) - timedelta(days=1)).date()


def _plural(n: int, forms: tuple) -> str:
    """Russian noun agreement. forms = (для 1, для 2-4, для 5+).
    Например: _plural(5, ('звонок','звонка','звонков')) → 'звонков'."""
    n10 = n % 10
    n100 = n % 100
    if n10 == 1 and n100 != 11:
        return forms[0]
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return forms[1]
    return forms[2]


_ACTIVITIES_FORMS = ("активность", "активности", "активностей")
_DEALS_FORMS = ("новая сделка", "новые сделки", "новых сделок")


def _format_talk_time(seconds: int) -> str:
    """Seconds → human-readable Russian: '45 мин' / '1 ч 23 мин' / '12 сек'."""
    if seconds < 60:
        return f"{seconds} сек"
    total_min = seconds // 60
    if total_min < 60:
        return f"{total_min} мин"
    h = total_min // 60
    m = total_min % 60
    return f"{h} ч {m} мин"


async def _resolve_manager_ids(b24: Bitrix24Client) -> Dict[int, str]:
    """Map name → bitrix user_id for the three sales managers."""
    users_map = await b24.get_users_map()
    out: Dict[int, str] = {}
    for uid, info in users_map.items():
        if info.get("name") in SALES_MANAGERS:
            out[uid] = info["name"]
    missing = set(SALES_MANAGERS) - set(out.values())
    if missing:
        logger.warning("Sales managers not resolved in Bitrix", missing=list(missing))
    return out


async def build_manager_daily_report(
    b24: Bitrix24Client, day: date
) -> str:
    """Return the formatted HTML report for the given day."""
    targets = await _resolve_manager_ids(b24)
    if not targets:
        return "<b>📊 Работа менеджеров</b>\n\nНе удалось получить состав отдела продаж из Bitrix."

    iso = day.isoformat()
    next_iso = (day + timedelta(days=1)).isoformat()
    manager_ids = list(targets.keys())

    activities = await b24.get_activities(
        assigned_by_ids=manager_ids,
        date_from=iso, date_to=iso,
        limit=500,
    )
    deals = await b24.get_deals(
        assigned_by_ids=manager_ids,
        filter_by_date_from=iso, filter_by_date_to=iso,
        limit=500,
    )
    if isinstance(deals, dict) and "items" in deals:
        deals = deals["items"]

    # Voximplant даёт реальную длительность разговора (CALL_DURATION в сек),
    # а не просто факт активности. crm.activity считает каждую попытку — даже
    # неотвеченную. Минуты считаем только по отвеченным звонкам.
    vox_calls = await b24.get_voximplant_stats(
        date_from=f"{iso}T00:00:00+03:00",
        date_to=f"{next_iso}T00:00:00+03:00",
        user_ids=manager_ids,
    )
    talk_seconds_by_mgr: Dict[int, int] = {}
    for c in vox_calls:
        uid = _safe_int(c.get("PORTAL_USER_ID"))
        if uid not in targets:
            continue
        dur = _safe_int(c.get("CALL_DURATION"))
        if dur > 0:
            talk_seconds_by_mgr[uid] = talk_seconds_by_mgr.get(uid, 0) + dur

    # Group activities by responsible × type
    by_mgr: Dict[int, Dict[int, int]] = {}
    for a in activities or []:
        rid = _safe_int(a.get("RESPONSIBLE_ID"))
        tid = _safe_int(a.get("TYPE_ID"))
        if rid not in targets:
            continue
        by_mgr.setdefault(rid, {})
        by_mgr[rid][tid] = by_mgr[rid].get(tid, 0) + 1

    # Count new deals per responsible
    deals_by_mgr: Dict[int, int] = {}
    for d in deals or []:
        rid = _safe_int(d.get("ASSIGNED_BY_ID"))
        if rid in targets:
            deals_by_mgr[rid] = deals_by_mgr.get(rid, 0) + 1

    title = day.strftime("%d.%m.%Y")
    lines: List[str] = [f"<b>📊 Работа менеджеров за {title}</b>", ""]

    # Render in the fixed roster order so the chat reads the same way every day.
    name_to_id = {v: k for k, v in targets.items()}
    for name in SALES_MANAGERS:
        uid = name_to_id.get(name)
        if uid is None:
            lines.append(f"<b>👤 {name}</b> — данных нет (не найден в Bitrix)")
            lines.append("")
            continue
        stats = by_mgr.get(uid, {})
        total = sum(stats.values())
        new_deals = deals_by_mgr.get(uid, 0)

        lines.append(
            f"<b>👤 {name} — {total} {_plural(total, _ACTIVITIES_FORMS)}, "
            f"{new_deals} {_plural(new_deals, _DEALS_FORMS)}</b>"
        )
        if total == 0 and new_deals == 0:
            lines.append("• активности за день не зафиксированы")
        else:
            # Show breakdown only for non-zero types, in fixed order.
            for tid, forms in _ACTIVITY_FORMS.items():
                n = stats.get(tid, 0)
                if not n:
                    continue
                label = _plural(n, forms)
                # На звонки добавляем реальную минутаж разговоров.
                if tid == 2:
                    talk_sec = talk_seconds_by_mgr.get(uid, 0)
                    if talk_sec > 0:
                        lines.append(
                            f"• <b>{n}</b> {label} "
                            f"(разговоров {_format_talk_time(talk_sec)})"
                        )
                    else:
                        lines.append(f"• <b>{n}</b> {label} (без записанных разговоров)")
                else:
                    lines.append(f"• <b>{n}</b> {label}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _safe_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


async def send_manager_daily(bot: Bot) -> None:
    """Cron entry — build yesterday's report and post it to the РОП chat."""
    if not settings.manager_daily_enabled or not settings.manager_daily_chat_id:
        logger.info("Manager daily report skipped — disabled or no chat id")
        return

    day = _yesterday_msk()
    b24 = Bitrix24Client()
    try:
        text = await build_manager_daily_report(b24, day)
    except Exception as e:
        logger.error("Manager daily report build failed", error=str(e))
        return
    finally:
        try:
            await b24.close()
        except Exception:
            pass

    try:
        await bot.send_message(
            settings.manager_daily_chat_id,
            text,
            parse_mode=ParseMode.HTML,
        )
        logger.info(
            "Manager daily report sent",
            chat_id=settings.manager_daily_chat_id,
            day=day.isoformat(),
            chars=len(text),
        )
    except Exception as e:
        logger.error("Manager daily report send failed", error=str(e))
