import aiohttp
import json
from typing import Dict, Any, List, Optional
import structlog
from config import settings
from b24.rate_limiter import RateLimiter

logger = structlog.get_logger()


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
        limit: int = 50,
        start: int = 0
    ) -> Dict[str, Any]:
        """Call Bitrix24 API method with rate limiting."""
        await self.rate_limiter.acquire()
        await self._ensure_session()

        url = f"{self.webhook_url}{method}.json"
        data = params or {}
        data["start"] = start
        data["limit"] = limit

        try:
            async with self._session.post(url, json=data) as resp:
                response = await resp.json()

                if not response.get("result"):
                    error_msg = response.get("error_description", "Unknown error")
                    logger.error("Bitrix24 API error", method=method, error=error_msg)
                    return {"error": error_msg}

                return response.get("result", {})
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
        """Paginate through API results."""
        all_items = []
        start = 0

        while len(all_items) < max_items:
            result = await self._call(method, params, limit=limit, start=start)

            if isinstance(result, dict) and "error" in result:
                break

            if isinstance(result, list):
                all_items.extend(result)
            elif isinstance(result, dict) and "result" in result:
                items = result["result"]
                if isinstance(items, list):
                    all_items.extend(items)
                else:
                    break
            else:
                break

            if len(result) < limit:
                break

            start += limit

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
            "select": ["*", "UF_*"]
        }

        if assigned_by_ids:
            params["filter"]["ASSIGNED_BY_ID"] = assigned_by_ids
        if filter_by_stage:
            params["filter"]["STAGE_ID"] = filter_by_stage
        if filter_by_date_from:
            params["filter"][">=DATE_CREATE"] = filter_by_date_from
        if filter_by_date_to:
            params["filter"]["<=DATE_CREATE"] = filter_by_date_to

        return await self._paginate("crm.deal.list", params, limit=limit, max_items=limit)

    async def get_deal(self, deal_id: int) -> Dict[str, Any]:
        """Get single deal details."""
        result = await self._call("crm.deal.get", {"id": deal_id})
        return result if not isinstance(result, dict) or "error" not in result else {}

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
            "select": ["*", "UF_*"]
        }

        if assigned_by_ids:
            params["filter"]["ASSIGNED_BY_ID"] = assigned_by_ids
        if filter_by_status:
            params["filter"]["STATUS_ID"] = filter_by_status
        if filter_by_date_from:
            params["filter"][">=DATE_CREATE"] = filter_by_date_from
        if filter_by_date_to:
            params["filter"]["<=DATE_CREATE"] = filter_by_date_to

        return await self._paginate("crm.lead.list", params, limit=limit, max_items=limit)

    async def get_lead(self, lead_id: int) -> Dict[str, Any]:
        """Get single lead details."""
        result = await self._call("crm.lead.get", {"id": lead_id})
        return result if not isinstance(result, dict) or "error" not in result else {}

    async def search_contacts(
        self,
        query: str,
        assigned_by_ids: List[int],
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search contacts by name/phone/email."""
        params = {
            "filter": {"NAME": query},
            "select": ["*", "PHONE", "EMAIL"]
        }

        if assigned_by_ids:
            params["filter"]["ASSIGNED_BY_ID"] = assigned_by_ids

        return await self._paginate("crm.contact.list", params, limit=limit, max_items=limit)

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        """Get contact details."""
        result = await self._call("crm.contact.get", {"id": contact_id})
        return result if not isinstance(result, dict) or "error" not in result else {}

    async def get_companies(
        self,
        assigned_by_ids: List[int],
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get companies."""
        params = {
            "filter": {},
            "select": ["*"]
        }

        if assigned_by_ids:
            params["filter"]["ASSIGNED_BY_ID"] = assigned_by_ids

        return await self._paginate("crm.company.list", params, limit=limit, max_items=limit)

    async def get_activities(
        self,
        assigned_by_ids: List[int],
        owner_id: int = None,
        date_from: str = None,
        date_to: str = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get activities (tasks, calls, meetings)."""
        params = {
            "filter": {},
            "select": ["*"]
        }

        if assigned_by_ids:
            params["filter"]["RESPONSIBLE_ID"] = assigned_by_ids

        if owner_id:
            params["filter"]["OWNER_ID"] = owner_id
        if date_from:
            params["filter"][">=CREATED"] = date_from
        if date_to:
            params["filter"]["<=CREATED"] = date_to

        return await self._paginate("crm.activity.list", params, limit=limit, max_items=limit)

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        """Get user details."""
        result = await self._call("user.get", {"ID": user_id})
        return result if not isinstance(result, dict) or "error" not in result else {}

    async def get_deal_stages(self) -> List[Dict[str, Any]]:
        """Get deal stages/pipeline."""
        result = await self._call("crm.dealcategory.stage.list", {})
        return result if isinstance(result, list) else []
