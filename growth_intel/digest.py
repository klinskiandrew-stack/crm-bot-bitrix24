"""Композиция воронки + триггеров + упущенной выручки в один AI-отчёт.

Возвращает готовый Telegram-HTML текст «где у нас слабые места и куда
менеджерам приложить руки прямо сейчас». Используется:
  • on-demand через tool growth_opportunities в боте.
  • cron-джобом раз в неделю (понедельник утром) → чат РОПа.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import structlog

from b24.client import Bitrix24Client
from config import settings
from growth_intel.funnel import build_funnel
from growth_intel.missed import missed_revenue_summary
from growth_intel.triggers import TRIGGER_CATEGORIES, analyze_deal
from sales_comms.collector import iter_active_deals

logger = structlog.get_logger()


# ---------- запуск анализатора триггеров по живым сделкам ----------------

async def _deals_with_recent_activity(since_hours: int) -> set:
    """ID-шники сделок, по которым были коммуникации за последние N часов.
    Источник — локальная deal_communications (наполняется часовым sync).
    Сильно сокращает кандидатов для DeepSeek-анализа в ежедневном
    отчёте: обычно 15-30 сделок вместо 100."""
    from db.connection import db
    rows = await db.fetch_all(
        f"""
        SELECT DISTINCT deal_id
        FROM deal_communications
        WHERE occurred_at >= datetime('now', '-{int(since_hours)} hours')
          AND deal_id IS NOT NULL
        """,
    )
    # sqlite3.Row не поддерживает .get() — обращаемся через индексацию.
    return {int(r["deal_id"]) for r in rows or []}


async def refresh_signals(
    client: Bitrix24Client,
    *,
    limit: int = 100,
    since_hours: Optional[int] = None,
) -> Dict[str, Any]:
    """Прогнать triggers.analyze_deal по живым сделкам.

    since_hours=None — полный прогон по всем активным (≈100 сделок,
    ~6 мин DeepSeek, ₽4/прогон). Подходит для ручного бэкфилла или
    еженедельного глубокого сканирования.

    since_hours=24/48 — инкремент: берём только сделки с новой
    активностью за последние N часов (обычно 15-30 в сутки). Сделки
    без движения уже разобраны прошлым прогоном — повторно гонять их
    бесполезно. Время и стоимость падают в 4-5 раз без потери качества.
    """
    deals = await iter_active_deals(client, max_items=limit)

    if since_hours is not None and since_hours > 0:
        active_ids = await _deals_with_recent_activity(since_hours)
        deals = [d for d in deals if int(d.get("ID") or 0) in active_ids]
        logger.info(
            "refresh_signals incremental mode",
            since_hours=since_hours, candidates=len(deals),
        )

    total_added = 0
    total_satisfied = 0
    processed = 0
    for d in deals:
        try:
            did = int(d["ID"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            opp = float(d.get("OPPORTUNITY") or 0)
        except (TypeError, ValueError):
            opp = 0.0
        mid = d.get("ASSIGNED_BY_ID")
        try:
            mid = int(mid) if mid is not None else None
        except (TypeError, ValueError):
            mid = None
        res = await analyze_deal(did, manager_id=mid, opportunity=opp)
        total_added += int(res.get("triggers_added") or 0)
        total_satisfied += int(res.get("satisfied_now") or 0)
        processed += 1
        await asyncio.sleep(0.1)   # лёгкий троттлинг, чтобы не упереться в DeepSeek rate
    return {
        "deals_scanned": processed,
        "new_triggers": total_added,
        "satisfied_now": total_satisfied,
    }


# ---------- сборка контекста для итогового LLM-обзора --------------------

def _format_funnel_block(funnel: Dict[str, Any]) -> str:
    lines = ["ВОРОНКА ПО МЕНЕДЖЕРАМ (за период)"]
    for name, m in funnel["managers"].items():
        if "error" in m:
            lines.append(f"  {name}: {m['error']}")
            continue
        b = m["buckets"]
        lines.append(
            f"  {name}: всего {m['total_deals']} сделок, "
            f"в работе {b.get('proposal',0)+b.get('measurement',0)+b.get('invoice',0)+b.get('created',0)}, "
            f"выиграно {b.get('won',0)} ({m['conversion_to_won_pct']}%), "
            f"проиграно {b.get('lost',0)}, выручка ₽{int(m['won_revenue']):,}".replace(",", " ")
        )
    tt = funnel["team_total"]
    lines.append(
        f"  ИТОГО ОТДЕЛ: {tt['total_deals']} сделок, "
        f"конверсия в продажу {tt['conversion_to_won_pct']}%, "
        f"выручка ₽{int(tt['won_revenue']):,}".replace(",", " ")
    )
    return "\n".join(lines)


def _format_signals_block(missed: Dict[str, Any], deal_titles: Dict[int, str], user_names: Dict[int, str]) -> str:
    lines = [
        f"НЕОТРАБОТАННЫЕ СИГНАЛЫ: {missed['total_signals']} шт, "
        f"под угрозой ₽{int(missed['total_at_risk_rub']):,} (high: ₽{int(missed['high_severity_at_risk_rub']):,})".replace(",", " ")
    ]
    if missed["by_category"]:
        cats = ", ".join(f"{k}={v}" for k, v in missed["by_category"].items())
        lines.append(f"  по категориям: {cats}")
    lines.append("")
    lines.append("ТОП сделок где висят сигналы (отсортированы по сумме):")
    for s in missed["top_signals"][:12]:
        did = s["deal_id"]
        title = deal_titles.get(did, "")[:40]
        mgr = user_names.get(s.get("manager_id"), "?")
        opp = int(s.get("value_at_risk") or 0)
        when = (s.get("detected_at") or "")[:10]
        cat = s.get("category", "?")
        lines.append(
            f"  #{did} «{title}» — ₽{opp:,} — {mgr} — {cat} ({when})\n     {s.get('evidence','')[:200]}".replace(",", " ")
        )
    return "\n".join(lines)


async def _enrich_top_signals(
    client: Bitrix24Client, missed: Dict[str, Any]
) -> Dict[int, str]:
    """Подтянуть TITLE для топ-сделок (нужно для читаемого дайджеста)."""
    ids = sorted({s["deal_id"] for s in missed.get("top_signals", [])})
    if not ids:
        return {}
    titles: Dict[int, str] = {}
    # Bulk fetch через filter ID
    items, _ = await client._paginate(
        "crm.deal.list",
        params={
            "filter": {"ID": ids},
            "select": ["ID", "TITLE"],
        },
        max_items=len(ids),
    )
    for d in items or []:
        try:
            titles[int(d["ID"])] = d.get("TITLE") or ""
        except (KeyError, ValueError):
            pass
    return titles


# ---------- финальный LLM-проход — текст отчёта --------------------------

_SYSTEM_PROMPT = """Ты — старший аналитик отдела продаж компании Growzone.
Тебе дают сырые цифры и факты по работе 3 менеджеров (Шеян Андрей,
Ребров Никита, Останина Любовь):
  1) воронка конверсии по менеджерам за период
  2) список неотработанных триггеров (где клиент дозрел, а менеджер
     ничего не сделал), с суммами сделок под риском
  3) топ конкретных сделок где скорее всего теряем деньги

Сформируй отчёт для руководителя отдела продаж в Telegram-HTML формате
(<b>...</b>, <i>...</i>). Структура:

<b>💰 Где растут деньги, где теряются — отчёт за период</b>

<b>1. Главное</b>
— одна-две фразы про общую картину: выручка/конверсия, динамика.
— цифра «упущенная выручка ₽X» (из total_at_risk).

<b>2. Топ-N сделок, где сейчас НАДО ДОЖИМАТЬ</b>
По 3-5 самых дорогих горящих сделок: ID, клиент, менеджер, что
конкретно сделать (счёт выставить / реквизиты ждём / перезвонить),
дедлайн.

<b>3. Где у каждого менеджера течёт воронка</b>
По каждому из 3 менеджеров: где конкретно проседает (стадия) и
гипотеза почему (если из триггеров видно — отрабатывает возражения
плохо? медленно отвечает? не предлагает следующий шаг?).

<b>4. Рекомендации на неделю</b>
3-5 конкретных действий для РОПа (не общих советов, а «Шеяну —
проконтролировать X», «Реброву — связаться с Y»).

Правила:
— Не выдумывай. Если в данных нет цифр — не пиши «конверсия упала».
— Russian. Telegram-HTML. Длина ≤3500 символов.
— Указывай ID сделок чтобы РОП их быстро находил.
"""


async def _call_deepseek_digest(context: str) -> str:
    if not settings.deepseek_api_key:
        return "⚠️ DeepSeek API не настроен — отчёт не собрать."
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "Сырые данные по отделу продаж:\n\n" + context[:40_000]},
        ],
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
        "max_tokens": 2500,
    }
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as s:
            async with s.post(url, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error("Growth digest LLM error", status=resp.status, body=text[:300])
                    return f"⚠️ DeepSeek вернул {resp.status}: {text[:200]}"
                data = json.loads(text)
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error("Growth digest LLM failed", error=str(e))
        return f"⚠️ Не удалось получить ответ от DeepSeek: {e}"


# ---------- публичная точка входа -----------------------------------------

async def build_growth_digest(
    *,
    client: Optional[Bitrix24Client] = None,
    period_days: int = 30,
    skip_refresh: bool = False,
    refresh_limit: int = 60,
    refresh_since_hours: Optional[int] = None,
) -> Dict[str, Any]:
    """Собрать отчёт «где растут деньги, где теряются».

    skip_refresh=True — не запускать analyze_deal перед сборкой
    (читать то что есть в growth_signals). Для on-demand вызовов из
    бота, чтобы не ждать 5-7 минут DeepSeek-проходов.

    refresh_since_hours=24 — инкрементальный режим: проходим только по
    сделкам с активностью за сутки. Используется ежедневным cron'ом —
    в 4-5 раз быстрее и дешевле полного скана.
    """
    own_client = client is None
    if own_client:
        client = Bitrix24Client()
    try:
        if not skip_refresh:
            logger.info("Growth digest: refreshing signals",
                        limit=refresh_limit, since_hours=refresh_since_hours)
            refresh = await refresh_signals(
                client, limit=refresh_limit, since_hours=refresh_since_hours,
            )
            logger.info("Growth digest: signals refreshed", **refresh)

        funnel = await build_funnel(
            client,
            date_from=date.today() - timedelta(days=period_days),
            date_to=date.today(),
        )
        missed = await missed_revenue_summary()
        deal_titles = await _enrich_top_signals(client, missed)
        users_map = await client.get_users_map()
        user_names = {uid: info.get("name", str(uid)) for uid, info in users_map.items()}

        # Контекст для DeepSeek — компактный текст, не JSON (LLM лучше
        # читает структурированный текст для нарратива).
        context = (
            _format_funnel_block(funnel)
            + "\n\n"
            + _format_signals_block(missed, deal_titles, user_names)
        )

        narrative = await _call_deepseek_digest(context)

        return {
            "text": narrative,
            "total_at_risk_rub": missed["total_at_risk_rub"],
            "signals_count": missed["total_signals"],
            "deals_in_funnel": funnel["team_total"]["total_deals"],
            "won_revenue": funnel["team_total"]["won_revenue"],
        }
    finally:
        if own_client:
            try:
                await client.close()
            except Exception:
                pass
