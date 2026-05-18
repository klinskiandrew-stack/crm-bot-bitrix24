"""Daily / weekly / monthly summary report generator.

All three reports share the same shape — only the date range differs:
  - Новые лиды (count, by source)
  - Качественные лиды = STATUS_ID=CONVERTED (count, by source)
  - Назначенные замеры = deals that hit STAGE_ID=NEW in 'Автополивы Сделки'
  - Заключённые договоры = deals that hit STAGE_ID=PREPARATION
    (plus per-deal list with amount + source — only in weekly/monthly)

Source grouping uses the YAML mapping from config/sources_mapping.yaml.
Anything not matched falls into 'Без источника'.
"""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
import yaml

from b24.client import Bitrix24Client

logger = structlog.get_logger()


# ---------- Source mapping ----------

_MAPPING_CACHE: Optional[Dict[str, Any]] = None


def _load_mapping() -> Dict[str, Any]:
    global _MAPPING_CACHE
    if _MAPPING_CACHE is not None:
        return _MAPPING_CACHE
    path = Path(__file__).resolve().parent.parent / "config" / "sources_mapping.yaml"
    if not path.exists():
        _MAPPING_CACHE = {}
        return _MAPPING_CACHE
    with open(path, encoding="utf-8") as f:
        _MAPPING_CACHE = yaml.safe_load(f) or {}
    return _MAPPING_CACHE


def _build_source_index() -> Tuple[Dict[str, str], List[Tuple[str, List[str]]]]:
    """Returns:
      - source_id -> channel name (exact match lookup)
      - list of (channel, phone_pool) for phone-substring fallback
    """
    mapping = _load_mapping()
    by_id: Dict[str, str] = {}
    phones: List[Tuple[str, List[str]]] = []
    for channel, cfg in mapping.items():
        if not isinstance(cfg, dict):
            continue
        for sid in (cfg.get("bitrix_source_ids") or []):
            by_id[str(sid)] = channel
        pool = cfg.get("phone_pool") or []
        if pool:
            phones.append((channel, [str(p).lstrip("+") for p in pool if p]))
    return by_id, phones


def _classify(record: Dict[str, Any], by_id: Dict[str, str], phones: List[Tuple[str, List[str]]]) -> str:
    """Classify one lead/deal into a channel name. Falls back to 'Без источника'."""
    sid = record.get("SOURCE_ID")
    if sid and str(sid) in by_id:
        return by_id[str(sid)]
    # Phone fallback — works for inbound calls with phone in TITLE
    title = (record.get("TITLE") or "")
    if title:
        title_lower = title.lower()
        for channel, pool in phones:
            for p in pool:
                if p and p in title_lower:
                    return channel
    return "Без источника"


# ---------- Bitrix data fetchers ----------

# Main funnel = 'Автополивы Сделки' (category 0)
_DEAL_CATEGORY = 0
_STAGE_MEASUREMENT_ASSIGNED = "NEW"        # Замер назначен (этап) — see prompts.py glossary
_STAGE_CONTRACT_SIGNED = "PREPARATION"      # Договор заключён (внесён аванс)


async def _fetch_leads(b24: Bitrix24Client, date_from: str, date_to: str) -> List[Dict[str, Any]]:
    """Get all leads created in the range, paginated up to 500."""
    leads = await b24.get_leads(
        assigned_by_ids=[],
        filter_by_date_from=date_from,
        filter_by_date_to=date_to,
        limit=500,
    )
    return leads if isinstance(leads, list) else []


async def _fetch_stage_passers(
    b24: Bitrix24Client,
    stage_id: str,
    date_from: str,
    date_to: str,
) -> List[Dict[str, Any]]:
    """Deals that landed on stage_id in main funnel during the period.
    Returns full deal dicts (TITLE, OPPORTUNITY, SOURCE_ID, ...) for unique
    OWNER_IDs from stagehistory."""
    history = await b24.get_stage_history(
        stage_id=stage_id,
        date_from=date_from,
        date_to=date_to,
        category_id=_DEAL_CATEGORY,
    )
    if "error" in history:
        logger.warning("stagehistory error", stage=stage_id, error=history["error"])
        return []
    unique_ids = history.get("unique_deal_ids") or []
    if not unique_ids:
        return []

    # Bulk fetch deal details via crm.deal.list with ID filter
    deals: List[Dict[str, Any]] = []
    for chunk_start in range(0, len(unique_ids), 50):
        chunk = unique_ids[chunk_start:chunk_start + 50]
        response = await b24._call("crm.deal.list", {
            "filter": {"ID": chunk},
            "select": ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "SOURCE_ID", "SOURCE_DESCRIPTION", "CATEGORY_ID"],
        })
        if isinstance(response, dict) and "error" in response:
            continue
        batch = response.get("result", []) if isinstance(response, dict) else []
        if isinstance(batch, list):
            deals.extend(batch)
    return deals


# ---------- Aggregation ----------

def _group_by_source(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """Group records by channel, return {channel: count} sorted desc."""
    by_id, phones = _build_source_index()
    counts: Dict[str, int] = defaultdict(int)
    for r in records:
        counts[_classify(r, by_id, phones)] += 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Russian noun pluralisation: 1 лид / 2 лида / 5 лидов."""
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 11 <= n_abs <= 14:
        return many
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def _format_money(amount: float) -> str:
    """Format 1200000.0 → '1 200 000 ₽'."""
    return f"{int(round(amount)):,} ₽".replace(",", " ")


def _format_section(title: str, total: int, breakdown: Dict[str, int], unit: Tuple[str, str, str]) -> str:
    """One section of the report.
    unit = (one, few, many) — e.g. ('лид', 'лида', 'лидов')."""
    plural = _ru_plural(total, *unit)
    head = f"<b>{title}</b> — {total} {plural}"
    if not breakdown:
        return head + "\n— нет данных"
    lines = [head]
    for channel, n in breakdown.items():
        if n == 0:
            continue
        lines.append(f"— {channel}: {n} {_ru_plural(n, *unit)}")
    return "\n".join(lines)


# ---------- Public API: build reports ----------

async def build_daily_report(b24: Bitrix24Client, day: date) -> str:
    """Daily report for one day (the given date)."""
    d = day.strftime("%Y-%m-%d")
    leads = await _fetch_leads(b24, d, d)
    qualified = [l for l in leads if l.get("STATUS_ID") == "CONVERTED"]
    measurements = await _fetch_stage_passers(b24, _STAGE_MEASUREMENT_ASSIGNED, d, d)
    contracts = await _fetch_stage_passers(b24, _STAGE_CONTRACT_SIGNED, d, d)

    contracts_sum = sum(float(c.get("OPPORTUNITY") or 0) for c in contracts)

    title = f"📊 <b>Отчёт за {day.strftime('%d.%m.%Y')}</b>"
    parts = [
        title,
        "",
        _format_section("Новые лиды", len(leads), _group_by_source(leads), ("лид", "лида", "лидов")),
        "",
        _format_section("Качественные лиды", len(qualified), _group_by_source(qualified), ("лид", "лида", "лидов")),
        "",
        _format_section("Назначенные замеры", len(measurements), _group_by_source(measurements), ("замер", "замера", "замеров")),
        "",
        (
            f"<b>Заключённые договоры</b> — {len(contracts)} "
            f"{_ru_plural(len(contracts), 'договор', 'договора', 'договоров')} "
            f"на общую сумму {_format_money(contracts_sum)}"
        ),
    ]
    if contracts:
        for ch, n in _group_by_source(contracts).items():
            if n:
                parts.append(f"— {ch}: {n} {_ru_plural(n, 'договор', 'договора', 'договоров')}")
    else:
        parts.append("— нет данных")
    return "\n".join(parts)


async def build_period_report(
    b24: Bitrix24Client,
    date_from: date,
    date_to: date,
    title_prefix: str,
    show_contract_list: bool = True,
) -> str:
    """Weekly / monthly report — same structure as daily plus optional
    per-contract list."""
    df = date_from.strftime("%Y-%m-%d")
    dt = date_to.strftime("%Y-%m-%d")
    leads = await _fetch_leads(b24, df, dt)
    qualified = [l for l in leads if l.get("STATUS_ID") == "CONVERTED"]
    measurements = await _fetch_stage_passers(b24, _STAGE_MEASUREMENT_ASSIGNED, df, dt)
    contracts = await _fetch_stage_passers(b24, _STAGE_CONTRACT_SIGNED, df, dt)

    contracts_sum = sum(float(c.get("OPPORTUNITY") or 0) for c in contracts)

    title = (
        f"📈 <b>{title_prefix} за "
        f"{date_from.strftime('%d.%m.%Y')} — {date_to.strftime('%d.%m.%Y')}</b>"
    )
    parts = [
        title,
        "",
        _format_section("Новые лиды", len(leads), _group_by_source(leads), ("лид", "лида", "лидов")),
        "",
        _format_section("Качественные лиды", len(qualified), _group_by_source(qualified), ("лид", "лида", "лидов")),
        "",
        _format_section("Назначенные замеры", len(measurements), _group_by_source(measurements), ("замер", "замера", "замеров")),
        "",
        (
            f"<b>Заключённые договоры</b> — {len(contracts)} "
            f"{_ru_plural(len(contracts), 'договор', 'договора', 'договоров')} "
            f"на общую сумму {_format_money(contracts_sum)}"
        ),
    ]
    if contracts:
        for ch, n in _group_by_source(contracts).items():
            if n:
                parts.append(f"— {ch}: {n} {_ru_plural(n, 'договор', 'договора', 'договоров')}")
    else:
        parts.append("— нет данных")

    # Per-contract list (only for weekly/monthly)
    if show_contract_list and contracts:
        parts.append("")
        parts.append(
            f"<b>Договоры за {date_from.strftime('%d.%m.%Y')} — "
            f"{date_to.strftime('%d.%m.%Y')}</b>"
        )
        by_id, phones = _build_source_index()
        # Sort by amount desc
        for c in sorted(contracts, key=lambda x: -float(x.get("OPPORTUNITY") or 0)):
            title_text = (c.get("TITLE") or "Без названия").strip()
            amount = _format_money(float(c.get("OPPORTUNITY") or 0))
            channel = _classify(c, by_id, phones)
            parts.append(f"— {title_text} — {amount}, {channel}")

    return "\n".join(parts)
