"""Воронка конверсии по менеджерам.

Для каждого менеджера отдела продаж за период строим простую таблицу:

    Создано → Замер → КП → Счёт → Выиграно
       100      55     32    18      8

С процентами конверсии стадия → стадия. Плюс сравнение с медианой по
отделу, чтобы видеть «у Шеяна слабая конверсия именно на стадии КП→
Счёт» (т.е. на ней теряет, и значит её надо разбирать).

Источник — `crm.deal.list` за период по каждому менеджеру + UF-причины
отказа из tool_handlers.DEAL_JUNK_REASONS. crm.stagehistory.list не
зовём — для MVP достаточно текущей стадии сделки (нужны не «прошёл
ли», а «сколько и где остановились» — это видно из STAGE_ID + CLOSED).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import structlog

from b24.client import Bitrix24Client
from growth_intel.stages import is_done, is_lost
from reports.manager_daily import SALES_MANAGERS

logger = structlog.get_logger()


# Семантические группы стадий — независимо от названия воронки.
# КРИТИЧЕСКОЕ БИЗНЕС-ПРАВИЛО Growzone: «продано» = договор заключён +
# аванс внесён (STAGE_ID=PREPARATION и далее). Стадия WON в Bitrix —
# это только финал по деньгам, а коммерческая победа уже на PREPARATION.
# См. growth_intel/stages.py.
_STAGE_BUCKETS_EARLY = [
    ("measurement", ["ЗАМЕР", "MEASUREMENT", "UC_PEXP", "UC_BFLJ2N", "NEW"]),
    ("proposal",    ["КП", "PROPOSAL", "OFFER", "UC_OFFER", "UC_LW3MC6", "UC_19II4Y"]),
    ("invoice",     ["СЧЁТ", "СЧЕТ", "INVOICE", "UC_INVOICE"]),
]


def _bucket_for_stage(stage_id: str) -> Optional[str]:
    """Stage → bucket. won/lost проверяем через stages.is_done / is_lost
    (учитывают бизнес-правило Growzone), остальные — по ключевым словам."""
    if is_done(stage_id):
        return "won"
    if is_lost(stage_id):
        return "lost"
    s = (stage_id or "").upper()
    for bucket, markers in _STAGE_BUCKETS_EARLY:
        for m in markers:
            if m in s:
                return bucket
    return "created"   # ранние/без явного маркера


async def _fetch_deals_for_manager(
    client: Bitrix24Client,
    manager_id: int,
    *,
    date_from: str,
    date_to: str,
) -> List[Dict[str, Any]]:
    """Сделки менеджера за период (по DATE_CREATE)."""
    items, _ = await client._paginate(
        "crm.deal.list",
        params={
            "filter": {
                "ASSIGNED_BY_ID": manager_id,
                ">=DATE_CREATE": date_from,
                "<DATE_CREATE": (
                    datetime.fromisoformat(date_to) + timedelta(days=1)
                ).strftime("%Y-%m-%d") if "T" not in date_to else date_to,
            },
            "select": [
                "ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "OPPORTUNITY",
                "CLOSED", "DATE_CREATE", "CLOSEDATE",
                "UF_CRM_67C71B6E2224F",  # причина отказа сделки
            ],
            "order": {"DATE_CREATE": "DESC"},
        },
        max_items=500,
    )
    return items if isinstance(items, list) else []


def _summarize_one(deals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Сводка по одному менеджеру: количество в каждом bucket'е + сумма
    выигранных + распределение причин отказа."""
    buckets = Counter()
    won_revenue = 0.0
    lost_reasons = Counter()
    for d in deals:
        bucket = _bucket_for_stage(d.get("STAGE_ID")) or "other"
        buckets[bucket] += 1
        if bucket == "won":
            try:
                won_revenue += float(d.get("OPPORTUNITY") or 0)
            except (TypeError, ValueError):
                pass
        if bucket == "lost":
            r_id = d.get("UF_CRM_67C71B6E2224F")
            lost_reasons[str(r_id)] += 1
    total = sum(buckets.values())
    conv_won = (buckets["won"] / total * 100) if total else 0.0
    return {
        "total_deals": total,
        "buckets": dict(buckets),
        "won_revenue": round(won_revenue, 0),
        "conversion_to_won_pct": round(conv_won, 1),
        "top_lost_reasons": [r for r, _ in lost_reasons.most_common(3)],
    }


async def build_funnel(
    client: Bitrix24Client,
    *,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> Dict[str, Any]:
    """Построить воронку по 3 менеджерам отдела продаж за период.

    По умолчанию — за последние 30 дней. Возвращает:
        {
          'period': {'from': ..., 'to': ...},
          'managers': {'Шеян Андрей': {...}, ...},
          'team_total': {...}
        }
    """
    if date_to is None:
        date_to = date.today()
    if date_from is None:
        date_from = date_to - timedelta(days=30)
    df = date_from.isoformat()
    dt = date_to.isoformat()

    users_map = await client.get_users_map()
    name_to_id = {info["name"]: uid for uid, info in users_map.items() if info.get("name") in SALES_MANAGERS}

    by_manager: Dict[str, Dict[str, Any]] = {}
    team_buckets = Counter()
    team_won_rev = 0.0
    for name in SALES_MANAGERS:
        uid = name_to_id.get(name)
        if uid is None:
            by_manager[name] = {"error": "не найден в Bitrix"}
            continue
        deals = await _fetch_deals_for_manager(client, uid, date_from=df, date_to=dt)
        summary = _summarize_one(deals)
        by_manager[name] = summary
        # Складываем в team total
        for b, n in summary["buckets"].items():
            team_buckets[b] += n
        team_won_rev += summary["won_revenue"]

    team_total = sum(team_buckets.values())
    team_conv = (team_buckets["won"] / team_total * 100) if team_total else 0.0

    return {
        "period": {"from": df, "to": dt},
        "managers": by_manager,
        "team_total": {
            "total_deals": team_total,
            "buckets": dict(team_buckets),
            "won_revenue": round(team_won_rev, 0),
            "conversion_to_won_pct": round(team_conv, 1),
        },
    }
