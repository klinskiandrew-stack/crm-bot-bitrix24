import aiohttp
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import structlog
from config import settings
from b24.rate_limiter import RateLimiter

logger = structlog.get_logger()


def _day_after(date_str: str) -> str:
    """Convert YYYY-MM-DD to next day for use as exclusive upper bound (<).

    Bitrix24 treats <=DATE_CREATE='2026-05-17' as <= 2026-05-17 00:00:00,
    excluding everything created later that same day. To include the full
    day, use <DATE_CREATE='2026-05-18' instead.
    """
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return (d + timedelta(days=1)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return date_str


class Bitrix24Client:
    def __init__(self, webhook_url: str = None):
        self.webhook_url = webhook_url or settings.b24_webhook_url
        self.rate_limiter = RateLimiter(max_requests=2, time_window=1)
        self._session = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    async def _ensure_session(self):
        """Ensure session is created."""
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def _call(
        self,
        method: str,
        params: Dict[str, Any] = None,
        start: int = 0
    ) -> Dict[str, Any]:
        """Call Bitrix24 API method with rate limiting. Returns full response dict."""
        await self.rate_limiter.acquire()
        await self._ensure_session()

        url = f"{self.webhook_url}{method}.json"
        data = dict(params or {})
        data["start"] = start

        try:
            async with self._session.post(url, json=data) as resp:
                response = await resp.json()

                if "error" in response or "error_description" in response:
                    error_msg = response.get("error_description") or response.get("error", "Unknown error")
                    logger.error("Bitrix24 API error", method=method, error=error_msg, response=str(response)[:300])
                    return {"error": error_msg}

                return response
        except Exception as e:
            logger.error("Bitrix24 request failed", method=method, error=str(e))
            return {"error": str(e)}

    async def _paginate(
        self,
        method: str,
        params: Dict[str, Any] = None,
        limit: int = 50,
        max_items: int = 500
    ) -> List[Dict[str, Any]]:
        """Paginate using Bitrix24 'next' field. Page size is always 50."""
        all_items = []
        start = 0
        PAGE_SIZE = 50

        while len(all_items) < max_items:
            response = await self._call(method, params, start=start)

            if isinstance(response, dict) and "error" in response:
                return {"error": response["error"]}

            batch = response.get("result", []) if isinstance(response, dict) else []
            if not isinstance(batch, list):
                break

            all_items.extend(batch)

            next_start = response.get("next") if isinstance(response, dict) else None
            if next_start is None or len(batch) < PAGE_SIZE:
                break
            start = next_start

        return all_items[:max_items]

    async def get_deals(
        self,
        assigned_by_ids: List[int],
        filter_by_stage: str = None,
        filter_by_date_from: str = None,
        filter_by_date_to: str = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get deals for assigned users."""
        params = {
            "filter": {},
            "select": [
                "ID", "TITLE", "STAGE_ID", "STAGE_SEMANTIC_ID", "IS_WON",
                "OPPORTUNITY", "CURRENCY_ID", "CLOSED",
                "DATE_CREATE", "BEGINDATE", "CLOSEDATE",
                "ASSIGNED_BY_ID", "CONTACT_ID", "COMPANY_ID",
                "TYPE_ID", "CATEGORY_ID"
            ]
        }

        if assigned_by_ids:
            params["filter"]["ASSIGNED_BY_ID"] = assigned_by_ids
        if filter_by_stage:
            params["filter"]["STAGE_ID"] = filter_by_stage
        if filter_by_date_from:
            params["filter"][">=DATE_CREATE"] = filter_by_date_from
        if filter_by_date_to:
            params["filter"]["<DATE_CREATE"] = _day_after(filter_by_date_to)

        return await self._paginate("crm.deal.list", params, limit=limit, max_items=limit)

    async def get_deal(self, deal_id: int) -> Dict[str, Any]:
        """Get single deal details."""
        response = await self._call("crm.deal.get", {"id": deal_id})
        if "error" in response:
            return response
        return response.get("result", {}) or {}

    async def get_leads(
        self,
        assigned_by_ids: List[int],
        filter_by_status: str = None,
        filter_by_date_from: str = None,
        filter_by_date_to: str = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get leads for assigned users."""
        params = {
            "filter": {},
            "select": [
                "ID", "TITLE", "STATUS_ID", "STATUS_SEMANTIC_ID",
                "OPPORTUNITY", "CURRENCY_ID",
                "DATE_CREATE", "DATE_MODIFY", "ASSIGNED_BY_ID",
                "NAME", "LAST_NAME", "COMPANY_TITLE", "SOURCE_ID"
            ]
        }

        if assigned_by_ids:
            params["filter"]["ASSIGNED_BY_ID"] = assigned_by_ids
        if filter_by_status:
            params["filter"]["STATUS_ID"] = filter_by_status
        if filter_by_date_from:
            params["filter"][">=DATE_CREATE"] = filter_by_date_from
        if filter_by_date_to:
            params["filter"]["<DATE_CREATE"] = _day_after(filter_by_date_to)

        return await self._paginate("crm.lead.list", params, limit=limit, max_items=limit)

    async def get_lead(self, lead_id: int) -> Dict[str, Any]:
        """Get single lead details."""
        response = await self._call("crm.lead.get", {"id": lead_id})
        if "error" in response:
            return response
        return response.get("result", {}) or {}

    async def search_contacts(
        self,
        query: str,
        assigned_by_ids: List[int],
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search contacts by name (substring) + phone/email multifields."""
        params = {
            "filter": {"%NAME": query},
            "select": [
                "ID", "NAME", "LAST_NAME", "SECOND_NAME",
                "COMPANY_TITLE", "POST", "ASSIGNED_BY_ID",
                "DATE_CREATE", "PHONE", "EMAIL"
            ]
        }

        if assigned_by_ids:
            params["filter"]["ASSIGNED_BY_ID"] = assigned_by_ids

        return await self._paginate("crm.contact.list", params, limit=limit, max_items=limit)

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        """Get contact details."""
        response = await self._call("crm.contact.get", {"id": contact_id})
        if "error" in response:
            return response
        return response.get("result", {}) or {}

    async def get_companies(
        self,
        assigned_by_ids: List[int],
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get companies."""
        params = {
            "filter": {},
            "select": [
                "ID", "TITLE", "COMPANY_TYPE", "INDUSTRY", "REVENUE",
                "CURRENCY_ID", "EMPLOYEES", "ASSIGNED_BY_ID", "DATE_CREATE"
            ]
        }

        if assigned_by_ids:
            params["filter"]["ASSIGNED_BY_ID"] = assigned_by_ids

        return await self._paginate("crm.company.list", params, limit=limit, max_items=limit)

    async def get_activities(
        self,
        assigned_by_ids: List[int],
        owner_id: int = None,
        owner_type_id: int = None,
        date_from: str = None,
        date_to: str = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get activities (tasks, calls, meetings)."""
        params = {
            "filter": {},
            "select": [
                "ID", "SUBJECT", "TYPE_ID", "STATUS", "COMPLETED",
                "RESPONSIBLE_ID", "OWNER_ID", "OWNER_TYPE_ID",
                "CREATED", "DEADLINE", "DESCRIPTION"
            ]
        }

        if assigned_by_ids:
            params["filter"]["RESPONSIBLE_ID"] = assigned_by_ids
        if owner_id and owner_type_id:
            params["filter"]["OWNER_ID"] = owner_id
            params["filter"]["OWNER_TYPE_ID"] = owner_type_id
        if date_from:
            params["filter"][">=CREATED"] = date_from
        if date_to:
            params["filter"]["<CREATED"] = _day_after(date_to)

        return await self._paginate("crm.activity.list", params, limit=limit, max_items=limit)

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        """Get user details."""
        response = await self._call("user.get", {"ID": user_id})
        if "error" in response:
            return response
        result = response.get("result", [])
        if isinstance(result, list):
            return result[0] if result else {}
        return result or {}

    async def get_deal_stages(self) -> List[Dict[str, Any]]:
        """Get all deal stages across pipelines via crm.status.list."""
        response = await self._call("crm.status.list", {"filter": {"ENTITY_ID": "DEAL_STAGE"}})
        if "error" in response:
            return []
        result = response.get("result", [])
        return result if isinstance(result, list) else []

    async def get_stage_history(
        self,
        stage_id: str,
        date_from: str,
        date_to: str,
        category_id: int = 0,
        entity_type_id: int = 2,
    ) -> Dict[str, Any]:
        """Get events where deals moved to a given stage in a date range.

        Uses crm.stagehistory.list — returns ALL events (not just current
        state), so we can count e.g. how many deals passed through 'Замер
        выполнен' even if they've since moved on.

        Returns dict: {events: [...], unique_owner_ids: set, total_events: int}.
        """
        params = {
            "entityTypeId": entity_type_id,
            "filter": {
                "=STAGE_ID": stage_id,
                "=CATEGORY_ID": category_id,
                ">=CREATED_TIME": f"{date_from}T00:00:00+03:00",
                "<CREATED_TIME": f"{_day_after(date_to)}T00:00:00+03:00",
            },
            "select": ["ID", "OWNER_ID", "STAGE_ID", "CREATED_TIME", "STAGE_SEMANTIC_ID"],
            "order": {"CREATED_TIME": "ASC"},
        }

        all_events = []
        start = 0
        PAGE = 50
        while True:
            response = await self._call("crm.stagehistory.list", params, start=start)
            if "error" in response:
                return {"error": response["error"]}
            items = response.get("result", {}).get("items", [])
            if not items:
                break
            all_events.extend(items)
            next_start = response.get("next")
            if next_start is None or len(items) < PAGE:
                break
            start = next_start
            if len(all_events) >= 500:  # safety cap
                break

        unique = sorted({e["OWNER_ID"] for e in all_events})
        return {
            "stage_id": stage_id,
            "category_id": category_id,
            "date_from": date_from,
            "date_to": date_to,
            "total_events": len(all_events),
            "unique_deal_count": len(unique),
            "unique_deal_ids": unique,
            "events": all_events[:50],  # sample only, to keep payload small
        }

    def deal_url(self, deal_id) -> str:
        """Build human-readable URL to a deal card in Bitrix24."""
        return f"{settings.b24_portal_url}/crm/deal/details/{deal_id}/"

    def lead_url(self, lead_id) -> str:
        """Build human-readable URL to a lead card in Bitrix24."""
        return f"{settings.b24_portal_url}/crm/lead/details/{lead_id}/"
