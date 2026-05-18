"""Tool handler implementations."""

import json
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List
import structlog
from b24.client import Bitrix24Client
from metrika.client import metrika_client
from sheets.lus_client import lus_client
from avito.client import avito_client
from config import settings

logger = structlog.get_logger()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
        is_admin = user_context.get("is_admin", False)

        if not b24_user_ids and not is_admin:
            return {"deals": [], "message": "No assigned users configured"}

        deals = await client.get_deals(
            assigned_by_ids=b24_user_ids,
            filter_by_stage=params.get("filter_by_stage"),
            filter_by_date_from=params.get("filter_by_date_from"),
            filter_by_date_to=params.get("filter_by_date_to"),
            filter_by_source_ids=params.get("filter_by_source_ids"),
            filter_by_title_contains=params.get("filter_by_title_contains"),
            filter_by_utm_source=params.get("filter_by_utm_source"),
            limit=min(params.get("limit", 20), 100)
        )

        if isinstance(deals, dict) and "error" in deals:
            return deals

        enriched = []
        if isinstance(deals, list):
            for d in deals[:50]:
                d = dict(d)
                d["card_url"] = client.deal_url(d.get("ID"))
                enriched.append(d)

        return {
            "count": len(enriched),
            "deals": enriched,
        }

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

        if not b24_user_ids and not user_context.get("is_admin", False):
            return {"leads": [], "message": "No assigned users configured"}

        err = _validate_params(params, stage_key="not_used")
        if err:
            return err

        leads = await client.get_leads(
            assigned_by_ids=b24_user_ids,
            filter_by_status=params.get("filter_by_status"),
            filter_by_date_from=params.get("filter_by_date_from"),
            filter_by_date_to=params.get("filter_by_date_to"),
            filter_by_source_ids=params.get("filter_by_source_ids"),
            filter_by_title_contains=params.get("filter_by_title_contains"),
            filter_by_utm_source=params.get("filter_by_utm_source"),
            limit=min(params.get("limit", 20), 100)
        )

        if isinstance(leads, dict) and "error" in leads:
            return leads

        enriched = []
        if isinstance(leads, list):
            for l in leads[:50]:
                l = dict(l)
                l["card_url"] = client.lead_url(l.get("ID"))
                enriched.append(l)

        return {
            "count": len(enriched),
            "leads": enriched,
        }

    async def search_contacts_or_companies(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Search contacts or companies."""
        client = await self._get_client()
        query = params.get("query", "").strip()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not query:
            return {"error": "query is required"}

        if not b24_user_ids and not user_context.get("is_admin", False):
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

        if not b24_user_ids and not user_context.get("is_admin", False):
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
        """Get user activity summary."""
        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids and not user_context.get("is_admin", False):
            return {"summary": {}, "message": "No assigned users configured"}

        date_from = params.get("date_from")
        date_to = params.get("date_to")

        # Get deals created in period
        new_deals = await client.get_deals(
            assigned_by_ids=b24_user_ids,
            filter_by_date_from=date_from,
            filter_by_date_to=date_to,
            limit=500
        )

        # Get activities
        activities = await client.get_activities(
            assigned_by_ids=b24_user_ids,
            date_from=date_from,
            date_to=date_to,
            limit=500
        )

        new_deals_count = len(new_deals) if isinstance(new_deals, list) else 0
        activities_count = len(activities) if isinstance(activities, list) else 0

        # Count activity types
        activity_types = {}
        if isinstance(activities, list):
            for activity in activities:
                atype = activity.get("TYPE_ID", "UNKNOWN")
                activity_types[atype] = activity_types.get(atype, 0) + 1

        return {
            "new_deals": new_deals_count,
            "activities_count": activities_count,
            "activity_types": activity_types,
            "period": {
                "from": date_from or "Not specified",
                "to": date_to or "Not specified"
            }
        }

    async def get_recent_activities(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get recent activities."""
        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids and not user_context.get("is_admin", False):
            return {"activities": [], "message": "No assigned users configured"}

        days_back = params.get("days_back", 7)
        date_from = (datetime.utcnow() - timedelta(days=days_back)).isoformat()

        activities = await client.get_activities(
            assigned_by_ids=b24_user_ids,
            date_from=date_from,
            limit=params.get("limit", 20)
        )

        if isinstance(activities, dict) and "error" in activities:
            return activities

        return {
            "count": len(activities) if isinstance(activities, list) else 0,
            "activities": activities[:20] if isinstance(activities, list) else [],
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


# Global handlers instance
handlers = ToolHandlers()
