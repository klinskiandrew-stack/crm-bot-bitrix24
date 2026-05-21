"""Cross-link a collected call with its Bitrix24 lead/deal.

Matches by the client's phone number, then pulls the current CRM state:
outcome (Квал/Неквал/В работе), deal stage, deal result, amount, whether
a measurement happened, refusal reason and the manager's comment.

Lets the lead-reports sheet show end-to-end analytics — what each call
turned into in the CRM.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

import structlog

from b24.client import Bitrix24Client

logger = structlog.get_logger()

# Stages of a non-default pipeline come prefixed 'C<N>:' (e.g. 'C5:LOSE').
_CAT_PREFIX = re.compile(r"^C\d+:")

# Bitrix24 deal stage code → name (crm.status.list ENTITY_ID=DEAL_STAGE).
# Single pipeline (CATEGORY_ID=0) — codes are unique portal-wide.
DEAL_STAGES: Dict[str, str] = {
    "UC_K1QCF0": "Квалификация пройдена",
    "UC_8R1J7B": "Подготовить предв. чертеж",
    "UC_LW3MC6": "Предварительное КП (до замера)",
    "NEW": "Замер назначен",
    "UC_BFLJ2N": "Замер выполнен",
    "UC_P5PQEP": "Подготовить чертежи",
    "UC_J5Q08F": "Подготовить смету",
    "UC_AOH41K": "Продажа оборудования",
    "UC_YKTFH9": "Партнеры ( Город )",
    "UC_KJ1AIQ": "Отложенный спрос АВТОПОЛИВ",
    "UC_CYCJGP": "Отлож.спрос ДРУГИЕ города",
    "UC_6T687I": "Отложенный спрос ФП",
    "UC_19II4Y": "Согласовать КП",
    "PREPARATION": "Договор заключен (внесен аванс)",
    "UC_5EL81I": "Оборудование закуплено",
    "UC_84WK2U": "Монтаж",
    "UC_BSBCZY": "Монтаж завершен",
    "UC_68KU96": "Монтаж завершен Филиалы",
    "WON": "Сделка завершена (остаток получен)",
    "LOSE": "Сделка провалена",
}

_MEASUREMENT_STAGE = "UC_BFLJ2N"  # «Замер выполнен»

# Dedicated CRM fields that record the on-site measurement on the DEAL.
# This is the reliable signal — the deal stage is NOT, because the funnel
# has side branches (Партнёры, Отложенный спрос) that skip the measurement
# stage entirely. UF_CRM_1741083174473 = «Дата замера»,
# UF_CRM_644F9F3198FD1 = «Дата и время замера».
_DEAL_MEASURE_FIELDS = ("UF_CRM_1741083174473", "UF_CRM_644F9F3198FD1")

# Reaching any of these means the contract is signed → deal "Успешна".
CONTRACT_STAGES = {
    "PREPARATION", "UC_5EL81I", "UC_84WK2U", "UC_BSBCZY", "UC_68KU96", "WON",
}

_EMPTY_VALUES = (None, "", "0", 0, False, [])


def _empty(reason: str = "Не найдено в CRM") -> Dict[str, Any]:
    """CRM block for a call with no matching card."""
    return {
        "b24_lead_id": None,
        "b24_deal_id": None,
        "crm_outcome": reason,
        "crm_deal_stage": "",
        "crm_deal_result": "",
        "crm_deal_amount": None,
        "crm_had_measurement": "",
        "crm_reason": "",
        "crm_manager_comment": "",
        "crm_card_url": "",
    }


def _stage_name(stage_id: str) -> str:
    """Human name for a deal stage, tolerating non-default pipelines.

    Stages of a non-default pipeline arrive prefixed 'C<N>:' (e.g.
    'C5:LOSE'). The standard codes (NEW/WON/LOSE/PREPARATION) are shared
    across pipelines, so we strip the prefix before the lookup; a truly
    unknown custom code is shown as-is rather than dropped.
    """
    if not stage_id:
        return ""
    if stage_id in DEAL_STAGES:
        return DEAL_STAGES[stage_id]
    return DEAL_STAGES.get(_CAT_PREFIX.sub("", stage_id), stage_id)


def _deal_result(stage_id: str, semantic: str) -> str:
    """Успешна (contract signed) / Провалена / В работе."""
    bare = _CAT_PREFIX.sub("", stage_id or "")
    if semantic == "F" or bare == "LOSE":
        return "Провалена"
    if bare in CONTRACT_STAGES or semantic == "S":
        return "Успешна"
    return "В работе"


async def _measurement(client: Bitrix24Client, deal: Dict[str, Any]) -> str:
    """'Был' if the deal has a measurement logged, else 'Не было'.

    Primary signal — the dedicated «Дата замера» CRM field. Fallback —
    the deal passed the «Замер выполнен» stage in its history (for deals
    where managers move the stage but don't fill the date field).
    """
    for field in _DEAL_MEASURE_FIELDS:
        if deal.get(field) not in _EMPTY_VALUES:
            return "Был"
    try:
        passed = await client.get_deal_passed_stages(int(deal["ID"]))
    except Exception as e:  # noqa: BLE001 — measurement is best-effort
        logger.warning("Stage history lookup failed", deal_id=deal.get("ID"), error=str(e))
        passed = set()
    return "Был" if _MEASUREMENT_STAGE in passed else "Не было"


async def _collect_deals(
    client: Bitrix24Client, lead_ids: List[int]
) -> List[Tuple[int, Dict[str, Any]]]:
    """All deals across every lead on the phone, as (lead_id, deal) pairs.

    A repeat caller's deal can hang off an older lead than the newest one,
    so we don't restrict the search to a single lead.
    """
    pairs: List[Tuple[int, Dict[str, Any]]] = []
    for lid in sorted(lead_ids, reverse=True):
        for deal in await client.get_deals_by_lead(lid):
            pairs.append((lid, deal))
    return pairs


async def enrich(report: Dict[str, Any], client: Bitrix24Client) -> Dict[str, Any]:
    """Resolve one call report to its CRM state. Returns the CRM block
    (the dict of crm_* fields) — always, even when nothing matched."""
    phone = (report.get("phone") or "").strip()
    if not phone:
        return _empty()

    lead_ids = await client.find_entity_by_phone(phone, "LEAD")
    if not lead_ids:
        return _empty()

    # Gather deals across ALL leads on this phone, then take the newest
    # deal — that's the client's furthest CRM progress.
    deal_pairs = await _collect_deals(client, lead_ids)
    deal_lead_id: Optional[int] = None
    deal: Optional[Dict[str, Any]] = None
    if deal_pairs:
        deal_lead_id, deal = max(deal_pairs, key=lambda p: int(p[1].get("ID", 0)))

    # Lead-level fields come from the deal's own lead when there's a deal,
    # otherwise from the newest lead on the phone.
    lead_id = deal_lead_id or max(lead_ids)
    lead = await client.get_lead(lead_id)
    if not isinstance(lead, dict) or not lead.get("ID"):
        return _empty()

    crm = _empty()
    crm["b24_lead_id"] = lead_id
    crm["crm_reason"] = (lead.get("UF_CRM_1723465843") or "").strip()
    crm["crm_manager_comment"] = (lead.get("COMMENTS") or "").strip()
    crm["crm_card_url"] = client.lead_url(lead_id)

    lead_status = lead.get("STATUS_ID")

    if deal:
        deal_id = int(deal["ID"])
        stage_id = deal.get("STAGE_ID") or ""
        semantic = deal.get("STAGE_SEMANTIC_ID") or ""
        crm["b24_deal_id"] = deal_id
        crm["crm_deal_stage"] = _stage_name(stage_id)
        crm["crm_deal_result"] = _deal_result(stage_id, semantic)
        try:
            crm["crm_deal_amount"] = float(deal.get("OPPORTUNITY") or 0) or None
        except (TypeError, ValueError):
            crm["crm_deal_amount"] = None
        crm["crm_card_url"] = client.deal_url(deal_id)
        crm["crm_had_measurement"] = await _measurement(client, deal)
    # No deal → measurement stays "" (blank): the column reads "Был"/"Не
    # было" only for calls that actually reached a deal, never for leads
    # that never converted.

    # Outcome: Квал if it became a deal or the lead is converted;
    # Неквал if the lead is junk; otherwise still В работе.
    if lead_status == "JUNK":
        crm["crm_outcome"] = "Неквал"
    elif deal or lead_status == "CONVERTED":
        crm["crm_outcome"] = "Квал"
    else:
        crm["crm_outcome"] = "В работе"

    logger.info(
        "CRM enrichment done",
        report_id=report.get("id"),
        lead_id=lead_id,
        deal_id=crm["b24_deal_id"],
        outcome=crm["crm_outcome"],
        measurement=crm["crm_had_measurement"],
    )
    return crm
