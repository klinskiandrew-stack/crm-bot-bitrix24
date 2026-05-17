"""Tool handler implementations."""

import json
from datetime import datetime, timedelta
from typing import Dict, Any, List
import structlog
from b24.client import Bitrix24Client

logger = structlog.get_logger()


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
            else:
                return {"error": f"Unknown tool: {tool_name}"}
        except Exception as e:
            logger.error("Tool handler error", tool=tool_name, error=str(e))
            return {"error": str(e)}

    async def get_deals(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get deals for assigned users."""
        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids:
            return {"deals": [], "message": "No assigned users configured"}

        deals = await client.get_deals(
            assigned_by_ids=b24_user_ids,
            filter_by_stage=params.get("filter_by_stage"),
            filter_by_date_from=params.get("filter_by_date_from"),
            filter_by_date_to=params.get("filter_by_date_to"),
            limit=min(params.get("limit", 50), 500)
        )

        if isinstance(deals, dict) and "error" in deals:
            return deals

        return {
            "count": len(deals) if isinstance(deals, list) else 0,
            "deals": deals[:50] if isinstance(deals, list) else []
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

        return {"deal": deal}

    async def get_leads(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get leads for assigned users."""
        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids:
            return {"leads": [], "message": "No assigned users configured"}

        leads = await client.get_leads(
            assigned_by_ids=b24_user_ids,
            filter_by_status=params.get("filter_by_status"),
            filter_by_date_from=params.get("filter_by_date_from"),
            filter_by_date_to=params.get("filter_by_date_to"),
            limit=min(params.get("limit", 50), 500)
        )

        if isinstance(leads, dict) and "error" in leads:
            return leads

        return {
            "count": len(leads) if isinstance(leads, list) else 0,
            "leads": leads[:50] if isinstance(leads, list) else []
        }

    async def search_contacts_or_companies(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Search contacts or companies."""
        client = await self._get_client()
        query = params.get("query", "").strip()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not query:
            return {"error": "query is required"}

        if not b24_user_ids:
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
        """Get pipeline summary."""
        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids:
            return {"summary": {}, "message": "No assigned users configured"}

        # Get all deals for the period
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

        # Group by stage
        summary = {}
        total_amount = 0

        for deal in deals:
            stage = deal.get("STAGE_ID", "UNKNOWN")
            amount = float(deal.get("OPPORTUNITY", 0) or 0)

            if stage not in summary:
                summary[stage] = {"count": 0, "amount": 0}

            summary[stage]["count"] += 1
            summary[stage]["amount"] += amount
            total_amount += amount

        return {
            "summary": summary,
            "total_amount": total_amount,
            "total_deals": len(deals)
        }

    async def get_user_activity_summary(self, params: Dict[str, Any], user_context: Dict[str, Any]) -> Dict[str, Any]:
        """Get user activity summary."""
        client = await self._get_client()
        b24_user_ids = user_context.get("b24_user_ids", [])

        if not b24_user_ids:
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

        if not b24_user_ids:
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
