"""Cross-link a collected call with its Bitrix24 lead/deal.

Matches by the client's phone number, then pulls the current CRM state:
outcome (Квал/Неквал/В работе), deal stage, deal result, amount, whether
a measurement happened, refusal reason and the manager's comment.

Lets the lead-reports sheet show end-to-end analytics — what each call
turned into in the CRM.
"""

from typing import Any, Dict, Optional

import structlog

from b24.client import Bitrix24Client

logger = structlog.get_logger()

# Bitrix24 deal stage code → name (crm.status.list ENTITY_ID=DEAL_STAGE).
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

# Reaching any of these means the contract is signed → deal "Успешна".
_CONTRACT_STAGES = {
    "PREPARATION", "UC_5EL81I", "UC_84WK2U", "UC_BSBCZY", "UC_68KU96", "WON",
}

# A deal sitting on any of these has logically passed the measurement,
# even without a stage-history record for UC_BFLJ2N.
_POST_MEASUREMENT_STAGES = {
    "UC_BFLJ2N", "UC_P5PQEP", "UC_J5Q08F", "UC_AOH41K", "UC_19II4Y",
} | _CONTRACT_STAGES


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


def _deal_result(stage_id: str, semantic: str) -> str:
    """Успешна (contract signed) / Провалена / В работе."""
    if semantic == "F" or stage_id == "LOSE":
        return "Провалена"
    if stage_id in _CONTRACT_STAGES or semantic == "S":
        return "Успешна"
    return "В работе"


async def enrich(report: Dict[str, Any], client: Bitrix24Client) -> Dict[str, Any]:
    """Resolve one call report to its CRM state. Returns the CRM block
    (the dict of crm_* fields) — always, even when nothing matched."""
    phone = (report.get("phone") or "").strip()
    if not phone:
        return _empty()

    lead_ids = await client.find_entity_by_phone(phone, "LEAD")
    if not lead_ids:
        return _empty()

    # Several leads on one phone (repeat callers) — take the newest.
    lead_id = max(lead_ids)
    lead = await client.get_lead(lead_id)
    if not isinstance(lead, dict) or not lead.get("ID"):
        return _empty()

    crm = _empty()
    crm["b24_lead_id"] = lead_id
    crm["crm_reason"] = (lead.get("UF_CRM_1723465843") or "").strip()
    crm["crm_manager_comment"] = (lead.get("COMMENTS") or "").strip()
    crm["crm_card_url"] = client.lead_url(lead_id)

    lead_status = lead.get("STATUS_ID")

    # Find the deal converted from this lead (newest if several).
    deals = await client.get_deals_by_lead(lead_id)
    deal = max(deals, key=lambda d: int(d.get("ID", 0))) if deals else None

    if deal:
        deal_id = int(deal["ID"])
        stage_id = deal.get("STAGE_ID") or ""
        semantic = deal.get("STAGE_SEMANTIC_ID") or ""
        crm["b24_deal_id"] = deal_id
        crm["crm_deal_stage"] = DEAL_STAGES.get(stage_id, stage_id)
        crm["crm_deal_result"] = _deal_result(stage_id, semantic)
        try:
            crm["crm_deal_amount"] = float(deal.get("OPPORTUNITY") or 0) or None
        except (TypeError, ValueError):
            crm["crm_deal_amount"] = None
        crm["crm_card_url"] = client.deal_url(deal_id)

        passed = await client.get_deal_passed_stages(deal_id)
        had_measure = (_MEASUREMENT_STAGE in passed) or (stage_id in _POST_MEASUREMENT_STAGES)
        crm["crm_had_measurement"] = "Был" if had_measure else "Не было"
    else:
        crm["crm_had_measurement"] = "Не было"

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
    )
    return crm
