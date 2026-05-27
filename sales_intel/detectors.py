"""Detectors that find where sales are leaking in Bitrix24.

Each detector pre-narrows the query server-side (by MOVED_TIME /
DATE_MODIFY), so we never pull the whole CRM — only the records that
are already suspicious. All times are handled in MSK, the company's
business timezone.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog

from b24.client import Bitrix24Client
from config import settings
from lead_reports.crm_enricher import CONTRACT_STAGES, _DEAL_MEASURE_FIELDS, _stage_name

logger = structlog.get_logger()

_MSK = timezone(timedelta(hours=3))

# Deal stages where a deal sits on purpose — deferred demand, not "stuck".
_PARKED_STAGES = {"UC_KJ1AIQ", "UC_CYCJGP", "UC_6T687I"}

# Lead statuses that aren't a live sales opportunity, so the cold-lead
# detector skips them:
#   UC_NXPDRA — «На следующий год» (deferred on purpose),
#   UC_A6P8IA — «По поводу работы» (job seekers, not customers),
#   UC_0TSBWN — «Монтажники полив» (contractor recruitment).
_LEAD_EXCLUDED_STATUSES = {"UC_NXPDRA", "UC_A6P8IA", "UC_0TSBWN"}

_EMPTY = (None, "", "0", 0, False, [])


def _now() -> datetime:
    return datetime.now(_MSK)


def _cutoff(days: int = 0) -> str:
    """ISO datetime `days` ago in MSK — for Bitrix24 `<=` date filters.
    A datetime (not bare date) is required so the cutoff is precise to
    the hour, not snapped to midnight."""
    dt = _now() - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+03:00")


def _parse_dt(raw: Any) -> Optional[datetime]:
    """Parse a Bitrix datetime ('2026-04-24T13:04:10+03:00'); always
    returns a timezone-aware datetime (assumes MSK if no offset)."""
    if not raw:
        return None
    s = str(raw)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("T", " ").split("+")[0].strip())
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_MSK)
    return dt


def _days_since(raw: Any) -> Optional[int]:
    dt = _parse_dt(raw)
    return None if dt is None else (_now() - dt).days


def _amount(deal: Dict[str, Any]) -> float:
    try:
        return float(deal.get("OPPORTUNITY") or 0)
    except (TypeError, ValueError):
        return 0.0


def _manager(uid: Any, users_map: Dict[int, Dict[str, str]]) -> str:
    """ASSIGNED_BY_ID → 'Фамилия Имя' ('' if empty, '#id' if unknown)."""
    try:
        key = int(uid)
    except (TypeError, ValueError):
        return ""
    if not key:
        return ""
    info = users_map.get(key)
    return info["name"] if info else f"#{key}"


def _measurement_done(deal: Dict[str, Any]) -> bool:
    """True if the deal's «Дата замера» is already in the past — the
    on-site measurement actually happened. A future date means it's
    only scheduled, so the deal isn't 'measurement stalled' yet."""
    now = _now()
    for field in _DEAL_MEASURE_FIELDS:
        dt = _parse_dt(deal.get(field))
        if dt is not None and dt <= now:
            return True
    return False


async def find_stuck_deals(
    client: Bitrix24Client,
    assigned_ids: List[int],
    stuck_days: int,
    measure_days: int,
    max_days: int,
) -> Dict[str, Any]:
    """Open deals in the SALES funnel whose stage hasn't moved for a while.

    Returns {"stuck": [...], "measurement_stalled": [...]}:
    - measurement_stalled — замер сделан (выезд инженера оплачен), но
      сделка не дошла до договора и стоит ≥ measure_days;
    - stuck — прочие открытые сделки до договора, застрявшие ≥ stuck_days.

    Scope filters keep the list actionable:
    - only CATEGORY_ID 0 — the main sales funnel (Монтаж / Сервис
      pipelines aren't sales opportunities);
    - contract stages and beyond are excluded — that sale is already won;
    - parked «отложенный спрос» stages are excluded — deferred on purpose;
    - deals idle longer than max_days are skipped — effectively dead.
    """
    deals = await client.get_deals(
        assigned_by_ids=assigned_ids,
        only_open=True,
        filter_by_moved_before=_cutoff(days=min(stuck_days, measure_days)),
        filter_by_moved_after=_cutoff(days=max_days),
        limit=1000,
    )
    if isinstance(deals, dict) and "error" in deals:
        logger.error("Stuck-deal query failed", error=deals["error"])
        return {"stuck": [], "measurement_stalled": [], "error": deals["error"]}

    users_map = await client.get_users_map()
    stuck: List[Dict[str, Any]] = []
    stalled: List[Dict[str, Any]] = []
    for d in deals:
        if str(d.get("CATEGORY_ID") or "0") != "0":
            continue  # only the main sales funnel
        stage_id = d.get("STAGE_ID") or ""
        if stage_id in _PARKED_STAGES or stage_id in CONTRACT_STAGES:
            continue  # parked, or already sold
        days = _days_since(d.get("MOVED_TIME") or d.get("DATE_MODIFY"))
        if days is None or days > max_days:
            continue
        item = {
            "id": d.get("ID"),
            "title": d.get("TITLE") or f"Сделка #{d.get('ID')}",
            "stage": _stage_name(stage_id),
            "amount": _amount(d),
            "manager": _manager(d.get("ASSIGNED_BY_ID"), users_map),
            "days_idle": days,
            "url": client.deal_url(d.get("ID")),
        }
        if _measurement_done(d) and days >= measure_days:
            stalled.append(item)
        elif days >= stuck_days:
            stuck.append(item)

    stuck.sort(key=lambda x: x["amount"], reverse=True)
    stalled.sort(key=lambda x: x["days_idle"], reverse=True)
    return {"stuck": stuck, "measurement_stalled": stalled}


async def find_cold_leads(
    client: Bitrix24Client, assigned_ids: List[int], cold_days: int, max_days: int
) -> Dict[str, Any]:
    """Active leads nobody has touched for cold_days+ — the 'forgotten' pile.

    The DATE_MODIFY window is bounded on BOTH ends: older than cold_days
    (gone quiet) but newer than max_days (still worth reviving — a lead
    untouched for years is dead, not forgotten). Leads younger than 3
    days are skipped — those belong to the speed-to-lead detector.
    """
    leads = await client.get_leads(
        assigned_by_ids=assigned_ids,
        filter_by_modified_before=_cutoff(days=cold_days),
        filter_by_modified_after=_cutoff(days=max_days),
        limit=1000,
    )
    if isinstance(leads, dict) and "error" in leads:
        logger.error("Cold-lead query failed", error=leads["error"])
        return {"leads": [], "error": leads["error"]}

    users_map = await client.get_users_map()
    out: List[Dict[str, Any]] = []
    for l in leads:
        # Skip converted / junk — only leads still genuinely in play.
        if (l.get("STATUS_SEMANTIC_ID") or "").strip() in ("S", "F"):
            continue
        if l.get("STATUS_ID") in _LEAD_EXCLUDED_STATUSES:
            continue  # «На следующий год» и т.п. — отложены намеренно
        created_days = _days_since(l.get("DATE_CREATE"))
        if created_days is not None and created_days < 3:
            continue  # fresh — covered by the speed-to-lead detector
        out.append({
            "id": l.get("ID"),
            "title": l.get("TITLE") or f"Лид #{l.get('ID')}",
            "manager": _manager(l.get("ASSIGNED_BY_ID"), users_map),
            "days_idle": _days_since(l.get("DATE_MODIFY")) or 0,
            "url": client.lead_url(l.get("ID")),
        })
    out.sort(key=lambda x: x["days_idle"], reverse=True)
    return {"leads": out}


async def find_untouched_new_leads(
    client: Bitrix24Client, assigned_ids: List[int], react_hours: int
) -> Dict[str, Any]:
    """Fresh leads (last 2 days) nobody has touched yet — speed-to-lead.

    'Untouched' = DATE_MODIFY within 5 min of DATE_CREATE (no manager
    action has bumped it), and the lead is already react_hours+ old.
    """
    since = (_now() - timedelta(days=2)).strftime("%Y-%m-%d")
    leads = await client.get_leads(
        assigned_by_ids=assigned_ids,
        filter_by_date_from=since,
        limit=500,
    )
    if isinstance(leads, dict) and "error" in leads:
        logger.error("New-lead query failed", error=leads["error"])
        return {"leads": [], "error": leads["error"]}

    users_map = await client.get_users_map()
    now = _now()
    out: List[Dict[str, Any]] = []
    for l in leads:
        if (l.get("STATUS_SEMANTIC_ID") or "").strip() in ("S", "F"):
            continue
        if l.get("STATUS_ID") in _LEAD_EXCLUDED_STATUSES:
            continue
        created = _parse_dt(l.get("DATE_CREATE"))
        if created is None:
            continue
        age_h = (now - created).total_seconds() / 3600
        if age_h < react_hours:
            continue
        modified = _parse_dt(l.get("DATE_MODIFY"))
        if modified and (modified - created).total_seconds() > 300:
            continue  # someone already worked it
        out.append({
            "id": l.get("ID"),
            "title": l.get("TITLE") or f"Лид #{l.get('ID')}",
            "manager": _manager(l.get("ASSIGNED_BY_ID"), users_map),
            "hours_idle": round(age_h, 1),
            "url": client.lead_url(l.get("ID")),
        })
    out.sort(key=lambda x: x["hours_idle"], reverse=True)
    return {"leads": out}


async def collect_opportunities(
    assigned_ids: List[int], client: Optional[Bitrix24Client] = None
) -> Dict[str, Any]:
    """Run every detector and return one combined result.

    assigned_ids=[] means 'no ASSIGNED_BY filter' — i.e. the whole CRM
    (used by the weekly digest and by admins).
    """
    own = client is None
    if own:
        client = Bitrix24Client()
        await client._ensure_session()
    try:
        stuck = await find_stuck_deals(
            client, assigned_ids,
            settings.stuck_deal_days, settings.measurement_followup_days,
            settings.stuck_deal_max_days,
        )
        cold = await find_cold_leads(
            client, assigned_ids,
            settings.cold_lead_days, settings.stuck_deal_max_days,
        )
        fresh = await find_untouched_new_leads(
            client, assigned_ids, settings.new_lead_react_hours,
        )
    finally:
        if own:
            await client.close()

    return {
        "measurement_stalled": stuck.get("measurement_stalled", []),
        "stuck_deals": stuck.get("stuck", []),
        "cold_leads": cold.get("leads", []),
        "untouched_leads": fresh.get("leads", []),
    }
