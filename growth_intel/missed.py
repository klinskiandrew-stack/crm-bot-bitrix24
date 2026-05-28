"""Расчёт упущенной выручки по неотработанным триггерам.

Бежит SQL по growth_signals — берём все unsatisfied + severity≥medium +
с указанным value_at_risk, агрегируем сумму и формируем топ-N сделок
где скорее всего теряем деньги.

Используется в digest.py и в инструменте бота growth_opportunities.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import structlog

from db.connection import db

logger = structlog.get_logger()


_OVERDUE_HORIZON_DAYS = 14    # сигналы старше этого срока в «топ» не идут — клиент уже остыл
_HOT_HORIZON_DAYS = 7         # если сигналу <= 7 дней — он горячий


async def missed_revenue_summary(
    *,
    only_categories: Optional[List[str]] = None,
    min_value: float = 0.0,
) -> Dict[str, Any]:
    """Считаем сумму value_at_risk по unsatisfied триггерам за последние
    N дней. Возвращает агрегаты + топ-сделки."""
    horizon = (datetime.now() - timedelta(days=_OVERDUE_HORIZON_DAYS)).isoformat()

    cat_filter = ""
    params: List[Any] = [horizon, min_value]
    if only_categories:
        placeholders = ",".join("?" * len(only_categories))
        cat_filter = f" AND category IN ({placeholders})"
        params.extend(only_categories)

    rows = await db.fetch_all(
        f"""
        SELECT id, deal_id, category, detected_at, deadline, evidence,
               value_at_risk, manager_id, severity
        FROM growth_signals
        WHERE satisfied = 0
          AND detected_at >= ?
          AND COALESCE(value_at_risk, 0) >= ?
          {cat_filter}
        ORDER BY value_at_risk DESC NULLS LAST, detected_at DESC
        """,
        tuple(params),
    )
    rows = [dict(r) for r in rows or []]

    total_at_risk = sum(float(r.get("value_at_risk") or 0) for r in rows)
    high_severity = [r for r in rows if r.get("severity") == "high"]
    high_at_risk = sum(float(r.get("value_at_risk") or 0) for r in high_severity)

    by_category: Dict[str, int] = {}
    by_manager: Dict[int, int] = {}
    for r in rows:
        by_category[r["category"]] = by_category.get(r["category"], 0) + 1
        mid = r.get("manager_id")
        if mid is not None:
            by_manager[mid] = by_manager.get(mid, 0) + 1

    return {
        "total_signals": len(rows),
        "total_at_risk_rub": round(total_at_risk, 0),
        "high_severity_signals": len(high_severity),
        "high_severity_at_risk_rub": round(high_at_risk, 0),
        "by_category": by_category,
        "by_manager": by_manager,
        "top_signals": rows[:15],   # для дайджеста — топ-15 сделок
    }


async def overdue_signals_by_manager(manager_id: int) -> List[Dict[str, Any]]:
    """Список горящих неотработанных сигналов по конкретному менеджеру.
    Используется когда РОП спрашивает «что у Реброва провисло»."""
    horizon = (datetime.now() - timedelta(days=_OVERDUE_HORIZON_DAYS)).isoformat()
    rows = await db.fetch_all(
        """
        SELECT id, deal_id, category, detected_at, deadline, evidence,
               value_at_risk, severity
        FROM growth_signals
        WHERE satisfied = 0
          AND manager_id = ?
          AND detected_at >= ?
        ORDER BY severity DESC, detected_at DESC
        """,
        (manager_id, horizon),
    )
    return [dict(r) for r in rows or []]
