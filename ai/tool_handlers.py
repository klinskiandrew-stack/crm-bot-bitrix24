"""Tool handler implementations."""

import json
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List
import structlog
from b24.client import Bitrix24Client

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
            else:
                return {"error": f"Unknown tool: {tool_name}"}
        except Exception as e:
            logger.error("Tool handler error", tool=tool_name, error=str(e))
            return {"error": str(e)}

    async def count_deals_passed_stage(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Count deals that passed through a given stage in a period.

        Uses history (crm.stagehistory.list) so we count deals that were
        on the stage at any moment during the period, even if they've
        since moved on or fallen out.
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

        # Enrich with card URLs for sample
        result["sample_deal_urls"] = [client.deal_url(d) for d in result["unique_deal_ids"][:10]]
        return result

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


# Global handlers instance
handlers = ToolHandlers()
