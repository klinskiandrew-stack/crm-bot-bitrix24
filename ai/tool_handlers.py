"""Tool handler implementations."""

import json
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List
import structlog
from b24.client import Bitrix24Client
from metrika.client import metrika_client
from sheets.lus_client import lus_client
from sheets.pnl_client import pnl_client
from avito.client import avito_client
from exports.leads_excel import build_leads_xlsx, LEAD_STATUS_RU
from config import settings

logger = structlog.get_logger()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ============================================================
# Bitrix24 UF enumeration dictionaries
# ============================================================
# Lead "Направление" (UF_CRM_1696239286) → human-readable name.
# Source: crm.lead.userfield.list ID=729.
LEAD_DIRECTIONS: Dict[int, str] = {
    85: "Автополив Москва и МО",
    87: "Фасадная подсветка",
    133: "Автополив другие города",
    151: "Ландшафтный дизайн",
    183: "Рулонный газон",
    599: "Ландшафтное освещение",
}

# Deal "Направление" (UF_CRM_651D2BA47419A) → human-readable name.
# Different IDs than leads — Bitrix assigns IDs per-field, not per-value.
# Source: crm.deal.userfield.list ID=731.
DEAL_DIRECTIONS: Dict[int, str] = {
    89: "Автополив Москва и МО",
    91: "Фасадная подсветка",
    137: "Автополив другие города",
    155: "Ландшафтный дизайн",
    187: "Рулонный газон",
    597: "Ландшафтное освещение",
}

# Deal "Причина отказа" (UF_CRM_67C71B6E2224F) → structured enum.
# Unlike leads (which use freeform UF_CRM_1723465843), deals refer to a
# curated list — perfect for clean grouping in analyze_junk_deals.
# Source: crm.deal.userfield.list ID=1159.
DEAL_JUNK_REASONS: Dict[int, str] = {
    279: "Недозвон",
    281: "СПАМ",
    283: "По поводу работы",
    285: "Маленький объём (менее 2 соток)",
    287: "Ошибся номером",
    289: "Дорого (газон, растения)",
    451: "Выбрали других",
    515: "Не актуально (продал/переехал, передумал)",
    551: "Дубль",
    553: "Тест",
    561: "Негатив от клиента",
    585: "Нет партнёра в этом регионе",
    593: "Ремонт системы Автополива",
    607: "Сделали сами",
    623: "Монтаж на свой материал",
    767: "Дорого (теплица, грядки)",
    775: "Тендер",
    783: "Предложение товаров/услуг",
    795: "Купить оборудование (малый объём)",
}


def _safe_int(val: Any) -> int:
    """Bitrix returns enum IDs as strings ('85') and sometimes ints (85)."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _resolve_direction(raw: Any, mapping: Dict[int, str]) -> str:
    """ID → Russian name, '' if unknown or empty. Falls back to '#<id>' for
    unknown enum values so the LLM still sees something searchable instead
    of silently dropping the row."""
    if raw in (None, "", 0, "0"):
        return ""
    key = _safe_int(raw)
    if not key:
        return ""
    return mapping.get(key, f"#{key}")


# Bitrix crm.activity TYPE_ID → Russian label.
ACTIVITY_TYPE_RU: Dict[int, str] = {
    1: "Встреча",
    2: "Звонок",
    3: "Задача",
    4: "Письмо",
    6: "Чат/мессенджер",
}


def _resolve_manager(uid: Any, users_map: Dict[int, Dict[str, str]]) -> str:
    """ASSIGNED_BY_ID / RESPONSIBLE_ID → 'Фамилия Имя'. Falls back to
    '#<id>' for unknown ids so the row is still identifiable."""
    key = _safe_int(uid)
    if not key:
        return ""
    info = users_map.get(key)
    return info["name"] if info else f"#{key}"


def _find_managers_by_name(name: str, users_map: Dict[int, Dict[str, str]]) -> List[tuple]:
    """Substring (case-insensitive) match of a manager name → list of
    (id, full_name). Used to turn 'Иванов' from the user's question into
    a concrete ASSIGNED_BY_ID filter."""
    q = (name or "").strip().lower()
    if not q:
        return []
    matches = []
    for uid, info in users_map.items():
        if q in info.get("name", "").lower():
            matches.append((uid, info["name"]))
    return matches


def _merge_assigned(b24_user_ids: List[int], manager_ids) -> List[int]:
    """Combine the access scope (b24_user_ids) with a requested manager
    filter. Empty scope = sees everyone, so the manager filter applies
    directly. A scoped user can only ever see managers inside their
    scope — hence the intersection. No overlap → [-1] (impossible id)
    so the query returns nothing instead of leaking everyone."""
    if not manager_ids:
        return b24_user_ids
    if not b24_user_ids:
        return list(manager_ids)
    overlap = [i for i in manager_ids if i in b24_user_ids]
    return overlap or [-1]


def _sees_all_crm(user_context: Dict[str, Any]) -> bool:
    """True if the user may read CRM data beyond their own b24_user_ids.

    Admins always can. The контекстолог (role 'partner') was granted
    read-only access to all leads/deals so they can analyse ad
    performance across every manager — see the access decision in the
    project notes. Empty b24_user_ids then means 'no ASSIGNED_BY filter'
    i.e. everything, exactly like an admin.
    """
    if user_context.get("is_admin"):
        return True
    return user_context.get("role") == "partner"


def _validate_params(params: Dict[str, Any], date_keys=("filter_by_date_from", "filter_by_date_to", "date_from", "date_to"), stage_key="filter_by_stage"):
    """Validate common Bitrix24 param formats. Returns None if OK, error dict if invalid."""
    for k in date_keys:
        v = params.get(k)
        if v and not DATE_RE.match(str(v)):
            return {"error": f"Параметр {k}='{v}' не соответствует формату YYYY-MM-DD"}
    stage = params.get(stage_key)
    if stage and ":" not in str(stage):
        return {"error": f"filter_by_stage='{stage}' имеет неверный формат, ожидается 'C{{N}}:CODE' (например 'C2:WON')"}
    return None


class ToolHandlers:
    """Handle tool calls from Claude."""

    def __init__(self):
        self.b24_client = None

    async def _get_client(self) -> Bitrix24Client:
        """Get or create Bitrix24 client."""
        if self.b24_client is None:
            self.b24_client = Bitrix24Client()
            await self.b24_client._ensure_session()
        return self.b24_client

    async def _resolve_manager_filter(self, params: Dict[str, Any]):
        """Turn the optional `manager_name` param into a list of manager IDs.

        Returns (manager_ids, error_dict):
        - (None, None)     — no manager_name given, nothing to filter
        - ([id, ...], None) — resolved successfully
        - (None, {error})  — not found or ambiguous; caller returns the error
        """
        name = params.get("manager_name")
        if not name:
            return None, None
        client = await self._get_client()
        users_map = await client.get_users_map()
        matches = _find_managers_by_name(name, users_map)
        if not matches:
            return None, {"error": f"Менеджер «{name}» не найден в CRM. Проверьте имя."}
        if len(matches) > 1:
            listed = ", ".join(f"{n}" for _, n in matches)
            return None, {
                "error": (
                    f"Под «{name}» подходит несколько сотрудников: {listed}. "
                    "Уточните, кто именно нужен."
                )
            }
        return [matches[0][0]], None

    async def handle_tool(self, tool_name: str, tool_input: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Route to appropriate tool handler."""
        try:
            if tool_name == "get_deals":
                return await self.get_deals(tool_input, user_context)
            elif tool_name == "get_deal_details":
                return await self.get_deal_details(tool_input, user_context)
            elif tool_name == "get_leads":
                return await self.get_leads(tool_input, user_context)
            elif tool_name == "search_contacts_or_companies":
                return await self.search_contacts_or_companies(tool_input, user_context)
            elif tool_name == "get_pipeline_summary":
                return await self.get_pipeline_summary(tool_input, user_context)
            elif tool_name == "get_user_activity_summary":
                return await self.get_user_activity_summary(tool_input, user_context)
            elif tool_name == "get_recent_activities":
                return await self.get_recent_activities(tool_input, user_context)
            elif tool_name == "count_deals_passed_stage":
                return await self.count_deals_passed_stage(tool_input, user_context)
            elif tool_name == "get_lead_full":
                return await self.get_lead_full(tool_input, user_context)
            elif tool_name == "get_deal_full":
                return await self.get_deal_full(tool_input, user_context)
            elif tool_name == "get_card_comments":
                return await self.get_card_comments(tool_input, user_context)
            elif tool_name == "analyze_junk_leads":
                return await self.analyze_junk_leads(tool_input, user_context)
            elif tool_name == "analyze_junk_deals":
                return await self.analyze_junk_deals(tool_input, user_context)
            elif tool_name == "export_leads_to_excel":
                return await self.export_leads_to_excel(tool_input, user_context)
            elif tool_name == "leads_summary":
                return await self.leads_summary(tool_input, user_context)
            elif tool_name == "metrika_traffic_summary":
                return await self.metrika_traffic_summary(tool_input, user_context)
            elif tool_name == "metrika_traffic_by_source":
                return await self.metrika_traffic_by_source(tool_input, user_context)
            elif tool_name == "lus_get_deal":
                return await self.lus_get_deal(tool_input, user_context)
            elif tool_name == "lus_search":
                return await self.lus_search(tool_input, user_context)
            elif tool_name == "lus_financials":
                return await self.lus_financials(tool_input, user_context)
            elif tool_name == "pnl_summary":
                return await self.pnl_summary(tool_input, user_context)
            elif tool_name == "pnl_articles":
                return await self.pnl_articles(tool_input, user_context)
            elif tool_name == "pnl_month":
                return await self.pnl_month(tool_input, user_context)
            elif tool_name == "avito_balance":
                return await self.avito_balance(tool_input, user_context)
            elif tool_name == "avito_items":
                return await self.avito_items(tool_input, user_context)
            elif tool_name == "avito_stats":
                return await self.avito_stats(tool_input, user_context)
            elif tool_name == "avito_spend":
                return await self.avito_spend(tool_input, user_context)
            elif tool_name == "avito_calls":
                return await self.avito_calls(tool_input, user_context)
            elif tool_name == "avito_funnel":
                return await self.avito_funnel(tool_input, user_context)
            elif tool_name == "avito_weak_ads":
                return await self.avito_weak_ads(tool_input, user_context)
            else:
                return {"error": f"Unknown tool: {tool_name}"}
        except Exception as e:
            logger.error("Tool handler error", tool=tool_name, error=str(e))
            return {"error": str(e)}

    async def count_deals_passed_stage(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Count deals that passed through a given stage in a period.

        Uses history (crm.stagehistory.list) so we count deals that were
        on the stage at any moment during the period, even if they've
        since moved on or fallen out. Enriches result with deal titles
        and card URLs so Claude can render cards without extra calls.
        """
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err

        stage_id = params.get("stage_id")
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        category_id = int(params.get("category_id", 0))

        if not stage_id or not date_from or not date_to:
            return {"error": "Параметры stage_id, date_from, date_to обязательны"}

        client = await self._get_client()
        result = await client.get_stage_history(
            stage_id=stage_id,
            date_from=date_from,
            date_to=date_to,
            category_id=category_id,
        )

        if "error" in result:
            return result

        # Fetch deal details (TITLE, STAGE_ID, OPPORTUNITY, etc.) for the unique IDs
        # so Claude doesn't need a follow-up call to render cards.
        deal_ids = result.get("unique_deal_ids", [])[:50]  # cap to avoid huge payloads
        users_map = await client.get_users_map()
        deals_brief = []
        for deal_id in deal_ids:
            try:
                deal = await client.get_deal(deal_id)
                if isinstance(deal, dict) and deal.get("ID"):
                    deals_brief.append({
                        "ID": deal["ID"],
                        "TITLE": deal.get("TITLE", f"Сделка #{deal_id}"),
                        "current_STAGE_ID": deal.get("STAGE_ID"),
                        "OPPORTUNITY": deal.get("OPPORTUNITY"),
                        "CURRENCY_ID": deal.get("CURRENCY_ID"),
                        "ASSIGNED_BY_ID": deal.get("ASSIGNED_BY_ID"),
                        "manager": _resolve_manager(deal.get("ASSIGNED_BY_ID"), users_map),
                        "card_url": client.deal_url(deal_id),
                    })
            except Exception as e:
                logger.warning("Failed to fetch deal detail", deal_id=deal_id, error=str(e))

        return {
            "stage_id": result["stage_id"],
            "category_id": result["category_id"],
            "date_from": result["date_from"],
            "date_to": result["date_to"],
            "total_transition_events": result["total_events"],
            "unique_deal_count": result["unique_deal_count"],
            "deals": deals_brief,
            "note": (
                "В deals — все уникальные сделки, побывавшие на стадии за период. "
                "current_STAGE_ID показывает где они находятся СЕЙЧАС "
                "(может отличаться от запрошенной стадии)."
            ),
        }

    async def get_deals(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get deals for assigned users."""
        err = _validate_params(params)
        if err:
            return err

        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids and not _sees_all_crm(user_context):
            return {"deals": [], "message": "No assigned users configured"}

        manager_ids, mgr_err = await self._resolve_manager_filter(params)
        if mgr_err:
            return mgr_err

        result = await client.get_deals(
            assigned_by_ids=_merge_assigned(b24_user_ids, manager_ids),
            filter_by_stage=params.get("filter_by_stage"),
            filter_by_date_from=params.get("filter_by_date_from"),
            filter_by_date_to=params.get("filter_by_date_to"),
            filter_by_source_ids=params.get("filter_by_source_ids"),
            filter_by_title_contains=params.get("filter_by_title_contains"),
            filter_by_utm_source=params.get("filter_by_utm_source"),
            filter_by_direction_ids=params.get("filter_by_direction_ids"),
            limit=min(params.get("limit", 20), 100),
            return_total=True,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        deals = result.get("items", [])
        total = result.get("total", len(deals))

        users_map = await client.get_users_map()
        enriched = []
        for d in deals[:50]:
            d = dict(d)
            d["card_url"] = client.deal_url(d.get("ID"))
            # Resolve UF enum IDs to human-readable names so the LLM
            # doesn't have to know magic numbers. Raw ID is kept too
            # for filter round-trips.
            d["direction"] = _resolve_direction(
                d.get("UF_CRM_651D2BA47419A"), DEAL_DIRECTIONS,
            )
            junk_reason_id = _safe_int(d.get("UF_CRM_67C71B6E2224F"))
            d["junk_reason"] = DEAL_JUNK_REASONS.get(junk_reason_id, "") if junk_reason_id else ""
            d["manager"] = _resolve_manager(d.get("ASSIGNED_BY_ID"), users_map)
            enriched.append(d)

        out = {
            "count": len(enriched),
            "total_in_crm": total,
            "deals": enriched,
        }
        if total > len(enriched):
            out["truncated"] = True
            out["note"] = (
                f"Показаны {len(enriched)} сделок из {total} подходящих под фильтр. "
                "НЕ называй количество показанных как итоговое — реальное число "
                "сделок = total_in_crm. Для точных подсчётов используй get_pipeline_summary."
            )
        return out

    async def get_deal_details(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get deal details."""
        client = await self._get_client()
        deal_id = params.get("deal_id")

        if not deal_id:
            return {"error": "deal_id is required"}

        deal = await client.get_deal(deal_id)

        if isinstance(deal, dict) and "error" in deal:
            return deal

        if isinstance(deal, dict) and deal.get("ID"):
            deal = dict(deal)
            deal["card_url"] = client.deal_url(deal["ID"])

        return {"deal": deal}

    async def get_leads(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get leads for assigned users."""
        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids and not _sees_all_crm(user_context):
            return {"leads": [], "message": "No assigned users configured"}

        err = _validate_params(params, stage_key="not_used")
        if err:
            return err

        manager_ids, mgr_err = await self._resolve_manager_filter(params)
        if mgr_err:
            return mgr_err

        result = await client.get_leads(
            assigned_by_ids=_merge_assigned(b24_user_ids, manager_ids),
            filter_by_status=params.get("filter_by_status"),
            filter_by_date_from=params.get("filter_by_date_from"),
            filter_by_date_to=params.get("filter_by_date_to"),
            filter_by_source_ids=params.get("filter_by_source_ids"),
            filter_by_title_contains=params.get("filter_by_title_contains"),
            filter_by_utm_source=params.get("filter_by_utm_source"),
            filter_by_direction_ids=params.get("filter_by_direction_ids"),
            limit=min(params.get("limit", 20), 100),
            return_total=True,
        )

        if isinstance(result, dict) and "error" in result:
            return result

        leads = result.get("items", [])
        total = result.get("total", len(leads))

        users_map = await client.get_users_map()
        enriched = []
        for l in leads[:50]:
            l = dict(l)
            l["card_url"] = client.lead_url(l.get("ID"))
            l["direction"] = _resolve_direction(
                l.get("UF_CRM_1696239286"), LEAD_DIRECTIONS,
            )
            l["manager"] = _resolve_manager(l.get("ASSIGNED_BY_ID"), users_map)
            enriched.append(l)

        out = {
            "count": len(enriched),
            "total_in_crm": total,
            "leads": enriched,
        }
        if total > len(enriched):
            out["truncated"] = True
            out["note"] = (
                f"Показаны {len(enriched)} лидов из {total} подходящих под фильтр. "
                "НЕ называй количество показанных как итоговое — реальное число "
                "лидов = total_in_crm. Для подсчётов и распределений используй leads_summary."
            )
        return out

    async def leads_summary(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Exact aggregate counts for leads — no per-row data.

        The right tool for "сколько лидов", "% неквала", "распределение по
        источникам/направлениям". get_leads only returns a capped page and
        is useless for counting; this fetches every matching lead (light
        select, paginated) and returns total + breakdowns. The LLM gets
        ~20 lines of aggregates instead of hundreds of rows.
        """
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err

        b24_user_ids = user_context.get("b24_user_ids", [])
        if not b24_user_ids and not _sees_all_crm(user_context):
            return {"summary": {}, "message": "No assigned users configured"}

        manager_ids, mgr_err = await self._resolve_manager_filter(params)
        if mgr_err:
            return mgr_err

        # High cap — a full year of Growzone leads is ~700. 5000 covers any
        # realistic question; beyond that we flag the aggregate as partial.
        HARD_CAP = 5000
        client = await self._get_client()
        result = await client.get_leads(
            assigned_by_ids=_merge_assigned(b24_user_ids, manager_ids),
            filter_by_status=params.get("filter_by_status"),
            filter_by_date_from=params.get("date_from"),
            filter_by_date_to=params.get("date_to"),
            filter_by_source_ids=params.get("source_ids"),
            filter_by_direction_ids=params.get("direction_ids"),
            limit=HARD_CAP,
            return_total=True,
        )
        if isinstance(result, dict) and "error" in result:
            return result

        leads = result.get("items", [])
        total = result.get("total", len(leads))
        users_map = await client.get_users_map()

        # Quality split by STATUS_SEMANTIC_ID — robust to custom UC_* codes:
        # S = converted (квал), F = junk (неквал), P/other = in progress.
        qualified = junk = in_progress = 0
        by_status: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        by_direction: Dict[str, int] = {}
        by_manager: Dict[str, int] = {}

        for lead in leads:
            semantic = (lead.get("STATUS_SEMANTIC_ID") or "").strip()
            if semantic == "S":
                qualified += 1
            elif semantic == "F":
                junk += 1
            else:
                in_progress += 1

            status_id = (lead.get("STATUS_ID") or "—").strip() or "—"
            status_label = LEAD_STATUS_RU.get(status_id, status_id)
            by_status[status_label] = by_status.get(status_label, 0) + 1

            src = (lead.get("SOURCE_ID") or "—").strip() or "—"
            by_source[src] = by_source.get(src, 0) + 1

            direction = _resolve_direction(lead.get("UF_CRM_1696239286"), LEAD_DIRECTIONS) or "—"
            by_direction[direction] = by_direction.get(direction, 0) + 1

            manager = _resolve_manager(lead.get("ASSIGNED_BY_ID"), users_map) or "—"
            by_manager[manager] = by_manager.get(manager, 0) + 1

        counted = len(leads)
        denom = counted or 1

        def _sorted(d: Dict[str, int]) -> Dict[str, int]:
            return dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True))

        summary = {
            "period": {"from": params.get("date_from"), "to": params.get("date_to")},
            "filters": {
                "status": params.get("filter_by_status"),
                "source_ids": params.get("source_ids"),
                "direction_ids": params.get("direction_ids"),
                "manager_name": params.get("manager_name"),
            },
            "total": total,
            "quality": {
                "qualified": qualified,
                "junk": junk,
                "in_progress": in_progress,
                "qual_rate_pct": round(qualified / denom * 100, 1),
                "junk_rate_pct": round(junk / denom * 100, 1),
            },
            "by_status": _sorted(by_status),
            "by_source": _sorted(by_source),
            "by_direction": _sorted(by_direction),
            "by_manager": _sorted(by_manager),
        }
        if total > counted:
            summary["partial"] = True
            summary["note"] = (
                f"Точный total = {total}, но разбивки посчитаны по первым {counted} "
                f"лидам (предел выборки {HARD_CAP}). Сузь период для точных разбивок."
            )
        return summary

    async def search_contacts_or_companies(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Search contacts or companies."""
        client = await self._get_client()
        query = params.get("query", "").strip()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not query:
            return {"error": "query is required"}

        if not b24_user_ids and not _sees_all_crm(user_context):
            return {"results": [], "message": "No assigned users configured"}

        contacts = await client.search_contacts(
            query=query,
            assigned_by_ids=b24_user_ids,
            limit=params.get("limit", 20)
        )

        if isinstance(contacts, dict) and "error" in contacts:
            return contacts

        return {
            "count": len(contacts) if isinstance(contacts, list) else 0,
            "results": contacts[:20] if isinstance(contacts, list) else []
        }

    async def get_pipeline_summary(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get pipeline summary — aggregates only, no raw deals in response."""
        err = _validate_params(params, stage_key="not_used")
        if err:
            return err

        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids and not _sees_all_crm(user_context):
            return {"summary": {}, "message": "No assigned users configured"}

        deals = await client.get_deals(
            assigned_by_ids=b24_user_ids,
            filter_by_date_from=params.get("date_from"),
            filter_by_date_to=params.get("date_to"),
            limit=500
        )

        if isinstance(deals, dict) and "error" in deals:
            return deals

        if not isinstance(deals, list):
            return {"summary": {}}

        # Group by stage AND semantic
        by_stage = {}
        by_semantic = {"P": {"count": 0, "amount": 0}, "S": {"count": 0, "amount": 0}, "F": {"count": 0, "amount": 0}}
        total_amount = 0.0

        for deal in deals:
            stage = deal.get("STAGE_ID", "UNKNOWN")
            sem = deal.get("STAGE_SEMANTIC_ID", "P")
            amount = float(deal.get("OPPORTUNITY", 0) or 0)

            if stage not in by_stage:
                by_stage[stage] = {"count": 0, "amount": 0.0}
            by_stage[stage]["count"] += 1
            by_stage[stage]["amount"] += amount

            if sem in by_semantic:
                by_semantic[sem]["count"] += 1
                by_semantic[sem]["amount"] += amount

            total_amount += amount

        return {
            "total_deals": len(deals),
            "total_amount": total_amount,
            "by_semantic": {
                "in_progress": by_semantic["P"],
                "won": by_semantic["S"],
                "lost": by_semantic["F"],
            },
            "by_stage": by_stage,
            "period": {
                "from": params.get("date_from") or "all",
                "to": params.get("date_to") or "all",
            }
        }

    async def get_user_activity_summary(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Per-manager activity breakdown for a period.

        Built for the РОП use case "как сегодня работают менеджеры": for
        each manager — new deals + activities split into звонки / встречи /
        задачи. Names are resolved from the user directory, never raw IDs.
        """
        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids and not _sees_all_crm(user_context):
            return {"managers": [], "message": "No assigned users configured"}

        manager_ids, mgr_err = await self._resolve_manager_filter(params)
        if mgr_err:
            return mgr_err
        assigned = _merge_assigned(b24_user_ids, manager_ids)

        date_from = params.get("date_from")
        date_to = params.get("date_to")

        new_deals = await client.get_deals(
            assigned_by_ids=assigned,
            filter_by_date_from=date_from,
            filter_by_date_to=date_to,
            limit=500,
        )
        activities = await client.get_activities(
            assigned_by_ids=assigned,
            date_from=date_from,
            date_to=date_to,
            limit=500,
        )
        if isinstance(new_deals, dict) and "error" in new_deals:
            return new_deals
        if isinstance(activities, dict) and "error" in activities:
            return activities
        new_deals = new_deals if isinstance(new_deals, list) else []
        activities = activities if isinstance(activities, list) else []

        users_map = await client.get_users_map()

        # Aggregate per manager id.
        stats: Dict[int, Dict[str, Any]] = {}

        def _slot(uid: int) -> Dict[str, Any]:
            return stats.setdefault(uid, {
                "manager": _resolve_manager(uid, users_map),
                "new_deals": 0,
                "activities": 0,
                "Звонок": 0, "Встреча": 0, "Задача": 0,
                "Письмо": 0, "Чат/мессенджер": 0, "Прочее": 0,
            })

        for d in new_deals:
            uid = _safe_int(d.get("ASSIGNED_BY_ID"))
            if uid:
                _slot(uid)["new_deals"] += 1

        for a in activities:
            uid = _safe_int(a.get("RESPONSIBLE_ID"))
            if not uid:
                continue
            slot = _slot(uid)
            slot["activities"] += 1
            type_label = ACTIVITY_TYPE_RU.get(_safe_int(a.get("TYPE_ID")), "Прочее")
            slot[type_label] = slot.get(type_label, 0) + 1

        # Sort by overall workload (deals + activities), busiest first.
        managers = sorted(
            stats.values(),
            key=lambda m: m["new_deals"] + m["activities"],
            reverse=True,
        )

        return {
            "period": {"from": date_from or "не указан", "to": date_to or "не указан"},
            "managers": managers,
            "totals": {
                "new_deals": len(new_deals),
                "activities": len(activities),
                "managers_active": len(managers),
            },
        }

    async def get_recent_activities(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get recent activities."""
        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids and not _sees_all_crm(user_context):
            return {"activities": [], "message": "No assigned users configured"}

        days_back = params.get("days_back", 7)
        date_from = (datetime.utcnow() - timedelta(days=days_back)).isoformat()

        manager_ids, mgr_err = await self._resolve_manager_filter(params)
        if mgr_err:
            return mgr_err

        activities = await client.get_activities(
            assigned_by_ids=_merge_assigned(b24_user_ids, manager_ids),
            date_from=date_from,
            limit=params.get("limit", 20)
        )

        if isinstance(activities, dict) and "error" in activities:
            return activities

        activities = activities if isinstance(activities, list) else []
        users_map = await client.get_users_map()
        enriched = []
        for a in activities[:20]:
            a = dict(a)
            a["responsible"] = _resolve_manager(a.get("RESPONSIBLE_ID"), users_map)
            a["type"] = ACTIVITY_TYPE_RU.get(_safe_int(a.get("TYPE_ID")), "Прочее")
            enriched.append(a)

        return {
            "count": len(activities),
            "activities": enriched,
            "period_days": days_back
        }

    # ---------- New tools: full card content ----------

    async def get_lead_full(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get a single lead with ALL fields (incl. custom UF_*) and comments
        from inside the card. Custom field codes are replaced with human names."""
        lead_id = params.get("lead_id")
        if not lead_id:
            return {"error": "lead_id is required"}

        client = await self._get_client()
        lead = await client.get_lead(int(lead_id))
        if isinstance(lead, dict) and "error" in lead:
            return lead
        if not lead:
            return {"error": f"Лид #{lead_id} не найден"}

        # Drop empty UF, rename to human names
        enriched = await client.enrich_with_uf_names(client.ENTITY_TYPE_LEAD, lead, drop_empty=True)
        enriched["card_url"] = client.lead_url(lead_id)

        # Trim long quiz answers in COMMENTS — they can be 1-2K each
        if isinstance(enriched.get("COMMENTS"), str) and len(enriched["COMMENTS"]) > 400:
            enriched["COMMENTS"] = enriched["COMMENTS"][:400] + "... [обрезано]"

        # Manager comments — fewer, shorter. Was 15×1000 = up to 15K just here.
        comments = await client.get_timeline_comments("lead", int(lead_id), limit=5)
        enriched["timeline_comments"] = [
            {
                "author_id": c.get("AUTHOR_ID"),
                "created": c.get("CREATED"),
                "text": c.get("COMMENT", "")[:300],
            }
            for c in comments
        ]
        return {"lead": enriched}

    async def get_deal_full(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get a single deal with ALL fields (incl. custom UF_*) and timeline comments."""
        deal_id = params.get("deal_id")
        if not deal_id:
            return {"error": "deal_id is required"}

        client = await self._get_client()
        deal = await client.get_deal(int(deal_id))
        if isinstance(deal, dict) and "error" in deal:
            return deal
        if not deal:
            return {"error": f"Сделка #{deal_id} не найдена"}

        enriched = await client.enrich_with_uf_names(client.ENTITY_TYPE_DEAL, deal, drop_empty=True)
        enriched["card_url"] = client.deal_url(deal_id)

        if isinstance(enriched.get("COMMENTS"), str) and len(enriched["COMMENTS"]) > 400:
            enriched["COMMENTS"] = enriched["COMMENTS"][:400] + "... [обрезано]"

        comments = await client.get_timeline_comments("deal", int(deal_id), limit=5)
        enriched["timeline_comments"] = [
            {
                "author_id": c.get("AUTHOR_ID"),
                "created": c.get("CREATED"),
                "text": c.get("COMMENT", "")[:300],
            }
            for c in comments
        ]
        return {"deal": enriched}

    async def analyze_junk_leads(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Aggregate JUNK leads for a period and group them by refusal reason.

        Reads UF_CRM_1723465843 (free-text reason filled by managers) from
        get_leads in one call and clusters near-duplicates. Replaces the
        previous LLM strategy of calling get_card_comments per lead, which
        burned the 8-call circuit breaker.

        Grouping is intentionally simple — strip whitespace, lowercase,
        cut multiline notes to the first meaningful line, dedup by that
        normalized key. Distinct phrasings ("дорого" vs "видимо дорого")
        stay separate; the LLM can merge them in the final answer.
        """
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err

        date_from = params.get("date_from")
        date_to = params.get("date_to")
        if not date_from or not date_to:
            return {"error": "date_from и date_to обязательны (формат YYYY-MM-DD)"}

        b24_user_ids = user_context.get("b24_user_ids", [])
        if not b24_user_ids and not _sees_all_crm(user_context):
            return {"summary": {}, "message": "No assigned users configured"}

        limit = min(int(params.get("limit") or 100), 200)
        top_n = max(1, int(params.get("top_n") or 8))

        client = await self._get_client()
        leads = await client.get_leads(
            assigned_by_ids=b24_user_ids,
            filter_by_status="JUNK",
            filter_by_date_from=date_from,
            filter_by_date_to=date_to,
            filter_by_source_ids=params.get("source_ids"),
            filter_by_title_contains=params.get("title_contains"),
            filter_by_direction_ids=params.get("direction_ids"),
            limit=limit,
        )

        if isinstance(leads, dict) and "error" in leads:
            return leads
        if not isinstance(leads, list):
            return {"error": "Bitrix вернул неожиданный формат"}

        total = len(leads)
        with_reason: List[Dict[str, Any]] = []
        without_reason_ids: List[int] = []

        def _normalize(raw: str) -> str:
            if not raw:
                return ""
            # Multiline notes ("18.05 - первое касание\n---ндз\n---ндз 2..." etc).
            # Keep just the first non-trivial line so duplicate notes collapse.
            first_line = next(
                (ln.strip(" -—\t").strip() for ln in raw.splitlines() if ln.strip(" -—\t").strip()),
                "",
            )
            return " ".join(first_line.lower().split())[:80]

        groups: Dict[str, Dict[str, Any]] = {}
        for lead in leads:
            lead_id = lead.get("ID")
            raw_reason = (lead.get("UF_CRM_1723465843") or "").strip()
            if not raw_reason:
                if lead_id:
                    without_reason_ids.append(int(lead_id))
                continue

            key = _normalize(raw_reason)
            if not key:
                if lead_id:
                    without_reason_ids.append(int(lead_id))
                continue

            entry = groups.setdefault(key, {
                "reason_example": raw_reason[:200],
                "count": 0,
                "lead_ids": [],
            })
            entry["count"] += 1
            if lead_id and len(entry["lead_ids"]) < 10:
                entry["lead_ids"].append(int(lead_id))

            with_reason.append({
                "id": lead_id,
                "title": (lead.get("TITLE") or "")[:80],
                "source": lead.get("SOURCE_ID"),
                "direction": _resolve_direction(lead.get("UF_CRM_1696239286"), LEAD_DIRECTIONS),
                "reason": raw_reason[:200],
                "card_url": client.lead_url(lead_id) if lead_id else None,
            })

        top_groups = sorted(groups.values(), key=lambda g: g["count"], reverse=True)[:top_n]
        examples = with_reason[:5]

        return {
            "period": {"from": date_from, "to": date_to},
            "filters": {
                "source_ids": params.get("source_ids"),
                "title_contains": params.get("title_contains"),
                "direction_ids": params.get("direction_ids"),
            },
            "summary": {
                "total_junk": total,
                "with_reason": len(with_reason),
                "without_reason": len(without_reason_ids),
                "unique_reason_groups": len(groups),
            },
            "top_reasons": top_groups,
            "examples": examples,
            "without_reason_lead_ids": without_reason_ids[:20],
            "note": (
                "top_reasons сгруппированы по нормализованной первой строке причины. "
                "Близкие по смыслу формулировки могут попасть в разные группы — "
                "объедини их в финальном ответе."
            ),
        }

    async def analyze_junk_deals(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Aggregate dropped deals over a period and group by curated reason.

        Mirrors analyze_junk_leads but for deals — and gets to use the
        structured enum UF_CRM_67C71B6E2224F instead of freeform notes,
        so grouping is exact (no fuzzy first-line trick needed).

        Filter by stage_semantic_id='F' (failed) by default, or pass
        stage_ids=['C0:LOSE','C2:LOSE',...] for a specific subset.
        """
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err

        date_from = params.get("date_from")
        date_to = params.get("date_to")
        if not date_from or not date_to:
            return {"error": "date_from и date_to обязательны (формат YYYY-MM-DD)"}

        b24_user_ids = user_context.get("b24_user_ids", [])
        if not b24_user_ids and not _sees_all_crm(user_context):
            return {"summary": {}, "message": "No assigned users configured"}

        limit = min(int(params.get("limit") or 100), 200)
        top_n = max(1, int(params.get("top_n") or 8))

        client = await self._get_client()
        deals = await client.get_deals(
            assigned_by_ids=b24_user_ids,
            filter_by_date_from=date_from,
            filter_by_date_to=date_to,
            filter_by_source_ids=params.get("source_ids"),
            filter_by_title_contains=params.get("title_contains"),
            filter_by_direction_ids=params.get("direction_ids"),
            limit=limit,
        )

        if isinstance(deals, dict) and "error" in deals:
            return deals
        if not isinstance(deals, list):
            return {"error": "Bitrix вернул неожиданный формат"}

        # Filter to dropped/failed deals locally (semantic 'F' = lose/junk
        # in Bitrix). get_deals doesn't expose stage_semantic directly as a
        # filter without naming a specific pipeline.
        dropped = [d for d in deals if d.get("STAGE_SEMANTIC_ID") == "F"]
        total = len(dropped)

        with_reason: List[Dict[str, Any]] = []
        without_reason_ids: List[int] = []
        groups: Dict[int, Dict[str, Any]] = {}

        for d in dropped:
            deal_id = d.get("ID")
            reason_id = _safe_int(d.get("UF_CRM_67C71B6E2224F"))
            if not reason_id:
                if deal_id:
                    without_reason_ids.append(int(deal_id))
                continue

            reason_name = DEAL_JUNK_REASONS.get(reason_id, f"#{reason_id}")
            entry = groups.setdefault(reason_id, {
                "reason_id": reason_id,
                "reason": reason_name,
                "count": 0,
                "deal_ids": [],
            })
            entry["count"] += 1
            if deal_id and len(entry["deal_ids"]) < 10:
                entry["deal_ids"].append(int(deal_id))

            with_reason.append({
                "id": deal_id,
                "title": (d.get("TITLE") or "")[:80],
                "source": d.get("SOURCE_ID"),
                "direction": _resolve_direction(d.get("UF_CRM_651D2BA47419A"), DEAL_DIRECTIONS),
                "reason": reason_name,
                "opportunity": d.get("OPPORTUNITY"),
                "card_url": client.deal_url(deal_id) if deal_id else None,
            })

        top_groups = sorted(groups.values(), key=lambda g: g["count"], reverse=True)[:top_n]

        return {
            "period": {"from": date_from, "to": date_to},
            "filters": {
                "source_ids": params.get("source_ids"),
                "title_contains": params.get("title_contains"),
                "direction_ids": params.get("direction_ids"),
            },
            "summary": {
                "total_dropped_deals": total,
                "with_reason": len(with_reason),
                "without_reason": len(without_reason_ids),
                "unique_reasons": len(groups),
            },
            "top_reasons": top_groups,
            "examples": with_reason[:5],
            "without_reason_deal_ids": without_reason_ids[:20],
            "note": (
                "Причины — структурированный enum из карточки сделки (19 значений), "
                "поэтому группировка точная, без слияния похожих формулировок."
            ),
        }

    async def export_leads_to_excel(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Build an .xlsx of leads and send it to the chat as a document.

        Unlike other tools, this one has a side effect: it pushes a file
        straight to Telegram via the bot handle injected into user_context
        as `_bot` / `_chat_id` by the message handler. The tool_result the
        LLM gets back is just a short confirmation string.

        Access: admins + the contextologist (role 'partner'). Today the
        only partner is the Директолог; if more partners are added later
        this opens to them too — tighten to an ID allowlist if needed.
        """
        is_admin = user_context.get("is_admin", False)
        role = user_context.get("role", "")
        if not (is_admin or role == "partner"):
            return {"error": "Выгрузка в Excel доступна только администраторам и директологу."}

        bot = user_context.get("_bot")
        chat_id = user_context.get("_chat_id")
        if bot is None or chat_id is None:
            return {"error": "Не удалось отправить файл — нет доступа к чату (внутренняя ошибка)."}

        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err

        b24_user_ids = user_context.get("b24_user_ids", [])
        if not b24_user_ids and not _sees_all_crm(user_context):
            return {"leads": [], "message": "No assigned users configured"}

        date_from = params.get("date_from")
        date_to = params.get("date_to")
        # Export limit is deliberately high — a month of leads can be 1000+.
        limit = min(int(params.get("limit") or 1000), 3000)

        client = await self._get_client()
        leads = await client.get_leads(
            assigned_by_ids=b24_user_ids,
            filter_by_status=params.get("filter_by_status"),
            filter_by_date_from=date_from,
            filter_by_date_to=date_to,
            filter_by_source_ids=params.get("source_ids"),
            filter_by_direction_ids=params.get("direction_ids"),
            include_full_utm=True,
            limit=limit,
        )

        if isinstance(leads, dict) and "error" in leads:
            return leads
        if not isinstance(leads, list):
            return {"error": "Bitrix вернул неожиданный формат"}

        if not leads:
            return {
                "status": "no_data",
                "message": "За указанный период/фильтр лидов не найдено — файл не отправлен.",
            }

        # Resolve enum direction to a label for each row (get_leads handler
        # does this too, but here we call the b24 client directly to pass
        # include_full_utm).
        for lead in leads:
            lead["direction"] = _resolve_direction(
                lead.get("UF_CRM_1696239286"), LEAD_DIRECTIONS,
            )
            lead["card_url"] = client.lead_url(lead.get("ID"))

        try:
            xlsx_bytes = build_leads_xlsx(leads)
        except Exception as e:
            logger.error("Excel build failed", error=str(e))
            return {"error": f"Не удалось сформировать Excel-файл: {e}"}

        # Latin filename — Cyrillic in document names is flaky across clients.
        if date_from and date_to:
            fname = f"leads_{date_from}_{date_to}.xlsx"
            period_label = f"{date_from} — {date_to}"
        else:
            fname = "leads_export.xlsx"
            period_label = "весь доступный период"

        caption = f"Лиды из CRM ({period_label}) — {len(leads)} шт."

        try:
            from aiogram.types import BufferedInputFile
            await bot.send_document(
                chat_id,
                document=BufferedInputFile(xlsx_bytes, filename=fname),
                caption=caption,
            )
        except Exception as e:
            logger.error("send_document failed", error=str(e), chat_id=chat_id)
            return {"error": f"Файл сформирован, но не отправился в чат: {e}"}

        logger.info("Leads Excel exported", count=len(leads), chat_id=chat_id, file=fname)
        return {
            "status": "sent",
            "lead_count": len(leads),
            "filename": fname,
            "message": (
                f"Файл {fname} с {len(leads)} лидами отправлен в чат. "
                "Коротко подтверди это пользователю, не пересказывай содержимое."
            ),
        }

    async def get_card_comments(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get manager-written comments from a lead or deal card timeline."""
        entity_type = (params.get("entity_type") or "").lower()
        entity_id = params.get("entity_id")
        if entity_type not in ("lead", "deal"):
            return {"error": "entity_type должен быть 'lead' или 'deal'"}
        if not entity_id:
            return {"error": "entity_id обязателен"}

        client = await self._get_client()
        comments = await client.get_timeline_comments(entity_type, int(entity_id), limit=20)
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "count": len(comments),
            "comments": [
                {
                    "author_id": c.get("AUTHOR_ID"),
                    "created": c.get("CREATED"),
                    "text": c.get("COMMENT", "")[:2000],
                }
                for c in comments
            ],
        }


    # ---------- Yandex Metrika tools ----------

    async def metrika_traffic_summary(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Visits/users/bounce rate/depth for a date range."""
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        if not date_from or not date_to:
            return {"error": "date_from и date_to обязательны (YYYY-MM-DD)"}
        return await metrika_client.get_traffic_summary(date_from, date_to)

    async def metrika_traffic_by_source(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Traffic breakdown by UTM source/medium/campaign or traffic channel."""
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        breakdown = (params.get("breakdown") or "utm_source").lower()
        limit = min(int(params.get("limit", 20)), 50)

        # UTM dimensions in Metrika REQUIRE last/first prefix — bare
        # ym:s:UTMSource returns 400 "Unknown dimension".
        dim_map = {
            "utm_source":   "ym:s:lastUTMSource",
            "utm_medium":   "ym:s:lastUTMMedium",
            "utm_campaign": "ym:s:lastUTMCampaign",
            "utm_content":  "ym:s:lastUTMContent",
            "utm_term":     "ym:s:lastUTMTerm",
            "channel":      "ym:s:trafficSource",  # canonical: direct/search/ad/social/referral
        }
        dimension = dim_map.get(breakdown)
        if not dimension:
            return {"error": f"breakdown должен быть одним из: {', '.join(dim_map)}"}

        return await metrika_client.get_traffic_by_source(date_from, date_to, dimension=dimension, limit=limit)


    # ---------- LUS Google Sheet tools ----------

    async def lus_get_deal(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Full row from the 'Сделки' tab by ID (порядковый номер 1-496)."""
        deal_id = params.get("id")
        if deal_id is None:
            return {"error": "id обязателен"}
        deal = await lus_client.get_deal(int(deal_id))
        if not deal:
            return {"error": f"Сделка #{deal_id} не найдена в таблице ЛУС"}
        return {"deal": deal}

    async def lus_search(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Search 'Сделки' by контрагент / номер договора / город (substring)."""
        query = (params.get("query") or "").strip()
        if not query:
            return {"error": "query обязателен"}
        limit = min(int(params.get("limit", 10)), 30)
        rows = await lus_client.search(query, limit=limit)
        return {"query": query, "found": len(rows), "rows": rows}

    async def lus_financials(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Aggregate financial KPIs from the 'Сделки' tab over a date range."""
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err
        group_by = params.get("group_by")
        allowed_groups = {
            "источник", "источник клиента",
            "направление", "услуга", "партнер", "партнёр", "месяц", "статус",
        }
        if group_by and group_by.lower() not in allowed_groups:
            return {"error": f"group_by должен быть одним из: {', '.join(sorted(allowed_groups))}"}
        # Normalise group_by to actual column name in sheet
        group_map = {
            "источник": "Источник клиента", "источник клиента": "Источник клиента",
            "направление": "Направление", "услуга": "Услуга",
            "партнер": "Партнер", "партнёр": "Партнер",
            "месяц": "Месяц", "статус": "Статус",
        }
        actual_group = group_map.get(group_by.lower()) if group_by else None
        only_completed = bool(params.get("only_completed", False))
        return await lus_client.financials(
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
            group_by=actual_group,
            only_completed=only_completed,
        )

    # ---------- ОПиУ P&L Google Sheet tools ----------

    async def pnl_summary(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """KPI-сводка из помесячных листов ОПиУ за последние N месяцев."""
        if not pnl_client.enabled:
            return {"error": "Google Sheet ОПиУ не настроен (нет ключа service account)"}
        months = int(params.get("months", 6) or 6)
        return await pnl_client.summary(months=months)

    async def pnl_articles(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Помесячная детализация по статьям ОПиУ за период."""
        if not pnl_client.enabled:
            return {"error": "Google Sheet ОПиУ не настроен"}
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        articles = params.get("articles")
        if articles is not None and not isinstance(articles, list):
            return {"error": "articles должен быть массивом строк"}
        return await pnl_client.articles(
            date_from=date_from,
            date_to=date_to,
            articles=articles,
        )

    async def pnl_month(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Полный ОПиУ за один месяц с разбивкой Москва/Казань/Итого."""
        if not pnl_client.enabled:
            return {"error": "Google Sheet ОПиУ не настроен"}
        ym = params.get("year_month")
        if not ym:
            return {"error": "year_month обязателен (формат YYYY-MM)"}
        return await pnl_client.month_detail(ym)

    async def avito_balance(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        if not avito_client.enabled:
            return {"error": "Avito не настроен (AVITO_CLIENT_ID/AVITO_CLIENT_SECRET пусты)"}
        return await avito_client.get_balance()

    async def avito_items(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        if not avito_client.enabled:
            return {"error": "Avito не настроен"}
        max_items = min(int(params.get("max_items", params.get("limit", 500))), 2000)
        return await avito_client.get_items_list(max_items=max_items)

    async def avito_stats(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        if not avito_client.enabled:
            return {"error": "Avito не настроен"}
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err
        return await avito_client.get_items_stats(
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
        )

    async def avito_spend(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        if not avito_client.enabled:
            return {"error": "Avito не настроен"}
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err
        return await avito_client.get_operations_history(
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
        )

    async def avito_calls(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        if not avito_client.enabled:
            return {"error": "Avito не настроен"}
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err
        return await avito_client.get_calls(
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
            limit=int(params.get("limit", 50)),
        )

    async def avito_weak_ads(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        if not avito_client.enabled:
            return {"error": "Avito не настроен"}
        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err
        return await avito_client.find_weak_and_star_ads(
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
            min_views_for_dead_zone=int(params.get("min_views_for_dead_zone", 20)),
        )

    async def avito_funnel(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Funnel: Avito spend → contacts → Bitrix leads → Bitrix deals → ROI.

        Связывает Avito API и Bitrix24 через SOURCE_ID из sources_mapping.yaml.
        Если у пользователя есть `b24_user_ids` (partner / scoped admin) — фильтрует
        только его лиды/сделки. Для admin без ограничений берёт всё.
        """
        if not avito_client.enabled:
            return {"error": "Avito не настроен"}

        err = _validate_params(params, date_keys=("date_from", "date_to"), stage_key="not_used")
        if err:
            return err

        date_from = params.get("date_from")
        date_to = params.get("date_to")

        # 1. Avito spend и contacts параллельно
        spend_data = await avito_client.get_operations_history(date_from, date_to)
        stats_data = await avito_client.get_items_stats(date_from, date_to)

        net_spend = spend_data.get("net_spend", 0) if "error" not in spend_data else 0
        total_deposit = spend_data.get("total_deposit", 0) if "error" not in spend_data else 0
        avito_contacts = stats_data.get("total_contacts", 0) if "error" not in stats_data else 0
        avito_views = stats_data.get("total_views", 0) if "error" not in stats_data else 0

        # 2. Bitrix24 leads + deals с источником Avito
        from ai.prompts import _load_sources_mapping
        mapping = _load_sources_mapping()
        avito_cfg = mapping.get("Авито") or mapping.get("Avito") or {}
        avito_source_ids = avito_cfg.get("bitrix_source_ids", [])
        phone_pool = avito_cfg.get("phone_pool", [])

        if not avito_source_ids:
            return {"error": "В config/sources_mapping.yaml не найден маппинг 'Авито'"}

        b24 = await self._get_client()
        assigned_ids = user_context.get("b24_user_ids") or []

        leads = await b24.get_leads(
            assigned_by_ids=assigned_ids,
            filter_by_date_from=date_from,
            filter_by_date_to=date_to,
            filter_by_source_ids=avito_source_ids,
            limit=500,
        )
        deals = await b24.get_deals(
            assigned_by_ids=assigned_ids,
            filter_by_date_from=date_from,
            filter_by_date_to=date_to,
            filter_by_source_ids=avito_source_ids,
            limit=500,
        )

        # 3. Метрики
        leads_count = len(leads)
        deals_count = len(deals)

        # Won сделки = STAGE_SEMANTIC_ID == 'S' (success)
        won_deals = [d for d in deals if d.get("STAGE_SEMANTIC_ID") == "S"]
        won_count = len(won_deals)
        won_revenue = sum(float(d.get("OPPORTUNITY") or 0) for d in won_deals)

        # Lost сделки
        lost_deals = [d for d in deals if d.get("STAGE_SEMANTIC_ID") == "F"]
        lost_count = len(lost_deals)

        # In-progress
        in_progress = deals_count - won_count - lost_count

        # Conversions / unit-economics
        def _safe_div(a, b):
            return round(a / b, 2) if b else None

        cpl_real = _safe_div(net_spend, leads_count)
        cac = _safe_div(net_spend, won_count)
        roi_x = _safe_div(won_revenue, net_spend)  # X-кратность
        roi_pct = round((won_revenue - net_spend) / net_spend * 100, 1) if net_spend else None

        conv_contact_to_lead = round(leads_count / avito_contacts * 100, 1) if avito_contacts else None
        conv_lead_to_won = round(won_count / leads_count * 100, 1) if leads_count else None
        conv_lead_to_deal = round(deals_count / leads_count * 100, 1) if leads_count else None

        # Топ сделок по сумме
        top_won = sorted(
            ({
                "id": d.get("ID"),
                "title": d.get("TITLE"),
                "amount": float(d.get("OPPORTUNITY") or 0),
                "closed_at": d.get("CLOSEDATE") or d.get("DATE_CREATE"),
                "assigned": d.get("ASSIGNED_BY_ID"),
            } for d in won_deals),
            key=lambda x: x["amount"],
            reverse=True,
        )[:10]

        return {
            "date_from": date_from,
            "date_to": date_to,
            "timezone": "Europe/Moscow (MSK)",
            "scope": "your data only" if assigned_ids else "all (admin)",

            "avito": {
                "net_spend_rub": net_spend,
                "total_deposit_rub": total_deposit,
                "views": avito_views,
                "contacts": avito_contacts,  # uniqContacts из API
                "view_to_contact_pct": _safe_div(avito_contacts * 100, avito_views),
            },

            "bitrix24": {
                "leads_count": leads_count,
                "deals_count": deals_count,
                "won_count": won_count,
                "lost_count": lost_count,
                "in_progress_count": in_progress,
                "won_revenue_rub": round(won_revenue, 0),
                "top_won_deals": top_won,
                "source_ids_used": avito_source_ids,
            },

            "funnel": {
                "avito_contacts": avito_contacts,
                "leads_in_bitrix": leads_count,
                "deals_in_bitrix": deals_count,
                "won_deals": won_count,
                "won_revenue_rub": round(won_revenue, 0),
            },

            "conversions_pct": {
                "contact_to_lead": conv_contact_to_lead,
                "lead_to_deal": conv_lead_to_deal,
                "lead_to_won": conv_lead_to_won,
            },

            "unit_economics_rub": {
                "cpl_real": cpl_real,       # стоимость одного лида в CRM
                "cac": cac,                 # стоимость одной выигранной сделки
                "roi_multiplier": roi_x,    # во сколько раз отбили
                "roi_pct": roi_pct,         # ROI в процентах
            },

            "note": (
                "CPL_real = расход_Avito / лиды_в_CRM (с SOURCE_ID Avito). "
                "ROI = выручка_won_сделок / расход. "
                "Сделки фильтруются по DATE_CREATE — могут включаться сделки лет назад "
                "если их источник Avito. Для чистой воронки конкретно за период "
                "лучше использовать avito_contacts vs leads_count."
            ),
        }


# Global handlers instance
handlers = ToolHandlers()
