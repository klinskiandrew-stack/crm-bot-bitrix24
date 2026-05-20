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
    # entityTypeId mapping for crm.item.fields
    ENTITY_TYPE_LEAD = 1
    ENTITY_TYPE_DEAL = 2
    ENTITY_TYPE_CONTACT = 3
    ENTITY_TYPE_COMPANY = 4

    def __init__(self, webhook_url: str = None):
        self.webhook_url = webhook_url or settings.b24_webhook_url
        self.rate_limiter = RateLimiter(max_requests=2, time_window=1)
        self._session = None
        # In-memory cache for UF field name maps (refreshed every 24h).
        # Shape: {entity_type_id: {"map": {UF_CRM_X: "Имя"}, "expires": datetime}}
        self._uf_meta_cache: Dict[int, Dict[str, Any]] = {}
        # In-memory cache for the Bitrix user directory (ID → name/position).
        # Refreshed every 30 min — staff list changes rarely.
        self._users_cache: Dict[int, Dict[str, str]] = {}
        self._users_cache_ts: float = 0.0

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

    async def _call_get(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """GET call — used for single-entity methods (crm.lead.get, crm.deal.get)
        where POST+JSON sometimes fails with 'ID is not defined or invalid'.
        Supports nested params via Bitrix24's filter[KEY]=value notation."""
        await self.rate_limiter.acquire()
        await self._ensure_session()

        url = f"{self.webhook_url}{method}"
        try:
            async with self._session.get(url, params=params or {}, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                try:
                    response = json.loads(text)
                except json.JSONDecodeError:
                    logger.error("Bitrix24 non-JSON response", method=method, status=resp.status, body=text[:200])
                    return {"error": f"Non-JSON response (status={resp.status})"}

                if "error" in response or "error_description" in response:
                    err = response.get("error_description") or response.get("error", "Unknown error")
                    logger.error("Bitrix24 API error", method=method, error=err)
                    return {"error": err}
                return response
        except Exception as e:
            logger.error("Bitrix24 GET failed", method=method, error=str(e))
            return {"error": str(e)}

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
    ):
        """Paginate using Bitrix24 'next' field. Page size is always 50.

        Returns a tuple (items, total) where `total` is Bitrix's count of
        ALL records matching the filter — even when we stop early at
        max_items. Callers that only need rows can ignore total; callers
        doing analytics ("how many leads") need it so they don't report a
        truncated page as the real number.

        On error returns ({"error": ...}, 0) — callers already isinstance-
        check the first element, so the shape stays compatible.
        """
        all_items = []
        start = 0
        total = 0
        PAGE_SIZE = 50

        while len(all_items) < max_items:
            response = await self._call(method, params, start=start)

            if isinstance(response, dict) and "error" in response:
                return {"error": response["error"]}, 0

            batch = response.get("result", []) if isinstance(response, dict) else []
            if not isinstance(batch, list):
                break

            # Bitrix returns the full match count in `total` on every page.
            if isinstance(response, dict) and response.get("total") is not None:
                total = int(response.get("total") or 0)

            all_items.extend(batch)

            next_start = response.get("next") if isinstance(response, dict) else None
            if next_start is None or len(batch) < PAGE_SIZE:
                break
            start = next_start

        items = all_items[:max_items]
        # If Bitrix didn't send total (rare), fall back to what we fetched.
        if not total:
            total = len(items)
        return items, total

    async def get_deals(
        self,
        assigned_by_ids: List[int],
        filter_by_stage: str = None,
        filter_by_date_from: str = None,
        filter_by_date_to: str = None,
        filter_by_source_ids: Optional[List[str]] = None,
        filter_by_title_contains: Optional[str] = None,
        filter_by_utm_source: Optional[str] = None,
        filter_by_direction_ids: Optional[List[int]] = None,
        limit: int = 50,
        return_total: bool = False,
    ):
        """Get deals for assigned users.

        Returns a list of deals by default. With return_total=True returns
        {"items": [...], "total": N} — N is Bitrix's count of ALL matching
        deals, even if the page was capped at `limit`. Use it for analytics
        so a truncated page isn't mistaken for the real number.

        Includes UTM fields by default so reports like 'deals by traffic
        source' don't need to fan out to get_deal_full per row.

        UF fields in select:
        - UF_CRM_651D2BA47419A = Направление (enumeration, see DEAL_DIRECTIONS
          in tool_handlers): 89=Автополив МСК, 91=Фасадная подсветка,
          187=Рулонный газон, 137=Автополив другие города, 155=Ландшафтный
          дизайн, 597=Ландшафтное освещение.
        - UF_CRM_67C71B6E2224F = Причина отказа сделки (enumeration, 19
          values — see DEAL_JUNK_REASONS).
        """
        params = {
            "filter": {},
            "select": [
                "ID", "TITLE", "STAGE_ID", "STAGE_SEMANTIC_ID", "IS_WON",
                "OPPORTUNITY", "CURRENCY_ID", "CLOSED",
                "DATE_CREATE", "BEGINDATE", "CLOSEDATE",
                "ASSIGNED_BY_ID", "CONTACT_ID", "COMPANY_ID",
                "TYPE_ID", "CATEGORY_ID", "SOURCE_ID", "SOURCE_DESCRIPTION",
                "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM",
                "UF_CRM_651D2BA47419A", "UF_CRM_67C71B6E2224F",
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
        if filter_by_source_ids:
            params["filter"]["SOURCE_ID"] = filter_by_source_ids
        if filter_by_title_contains:
            params["filter"]["%TITLE"] = filter_by_title_contains
        if filter_by_utm_source:
            params["filter"]["UTM_SOURCE"] = filter_by_utm_source
        if filter_by_direction_ids:
            # Bitrix accepts arrays for UF enum filter — matches any of given IDs.
            params["filter"]["UF_CRM_651D2BA47419A"] = filter_by_direction_ids

        items, total = await self._paginate("crm.deal.list", params, limit=limit, max_items=limit)
        if isinstance(items, dict) and "error" in items:
            return items  # propagate error dict unchanged
        if return_total:
            return {"items": items, "total": total}
        return items

    async def get_deal(self, deal_id: int) -> Dict[str, Any]:
        """Get single deal details. Uses GET because POST+JSON sometimes
        returns 'ID is not defined or invalid' for crm.deal.get."""
        response = await self._call_get("crm.deal.get", {"ID": deal_id})
        if "error" in response:
            return response
        return response.get("result", {}) or {}

    async def get_leads(
        self,
        assigned_by_ids: List[int],
        filter_by_status: str = None,
        filter_by_date_from: str = None,
        filter_by_date_to: str = None,
        filter_by_source_ids: Optional[List[str]] = None,
        filter_by_title_contains: Optional[str] = None,
        filter_by_utm_source: Optional[str] = None,
        filter_by_direction_ids: Optional[List[int]] = None,
        include_full_utm: bool = False,
        limit: int = 50,
        return_total: bool = False,
    ):
        """Get leads for assigned users.

        Returns a list of leads by default. With return_total=True returns
        {"items": [...], "total": N} — N is Bitrix's count of ALL matching
        leads, even if the page was capped at `limit`. Analytics callers
        ("how many leads from X") must use it so a truncated page isn't
        reported as the real count.

        Lean select — only fields needed for typical reports. Removed
        DATE_MODIFY/LAST_NAME/COMPANY_TITLE/CURRENCY_ID/UTM_CONTENT/UTM_TERM
        to shrink payload ~35%. For full data use get_lead_full per ID.

        include_full_utm=True adds UTM_CONTENT + UTM_TERM — the ad-creative
        and keyword fields the contextologist needs for the Excel export.
        They're left out of the default select to keep report payloads small.

        UF fields kept in lean select:
        - UF_CRM_1723465843 = freeform "причина отказа" (refusal reason),
          filled by managers on JUNK leads. Lets analyze_junk_leads / a single
          get_leads call group refusals without fanning out to get_card_comments.
        - UF_CRM_1696239286 = Направление (enumeration, see LEAD_DIRECTIONS
          in tool_handlers): 85=Автополив МСК, 87=Фасадная подсветка,
          183=Рулонный газон, 133=Автополив другие города, 151=Ландшафтный
          дизайн, 599=Ландшафтное освещение.
        """
        select = [
            "ID", "TITLE", "STATUS_ID", "STATUS_SEMANTIC_ID",
            "OPPORTUNITY",
            "DATE_CREATE", "ASSIGNED_BY_ID", "NAME",
            "SOURCE_ID", "SOURCE_DESCRIPTION", "WEBFORM_ID",
            "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN",
            "UF_CRM_1723465843", "UF_CRM_1696239286",
        ]
        if include_full_utm:
            select += ["UTM_CONTENT", "UTM_TERM"]
        params = {
            "filter": {},
            "select": select,
        }

        if assigned_by_ids:
            params["filter"]["ASSIGNED_BY_ID"] = assigned_by_ids
        if filter_by_status:
            params["filter"]["STATUS_ID"] = filter_by_status
        if filter_by_date_from:
            params["filter"][">=DATE_CREATE"] = filter_by_date_from
        if filter_by_date_to:
            params["filter"]["<DATE_CREATE"] = _day_after(filter_by_date_to)
        if filter_by_source_ids:
            # Bitrix accepts arrays for SOURCE_ID — matches any of given values
            params["filter"]["SOURCE_ID"] = filter_by_source_ids
        if filter_by_title_contains:
            params["filter"]["%TITLE"] = filter_by_title_contains
        if filter_by_utm_source:
            params["filter"]["UTM_SOURCE"] = filter_by_utm_source
        if filter_by_direction_ids:
            params["filter"]["UF_CRM_1696239286"] = filter_by_direction_ids

        items, total = await self._paginate("crm.lead.list", params, limit=limit, max_items=limit)
        if isinstance(items, dict) and "error" in items:
            return items  # propagate error dict unchanged
        if return_total:
            return {"items": items, "total": total}
        return items

    async def get_lead(self, lead_id: int) -> Dict[str, Any]:
        """Get single lead details. Uses GET — see get_deal note."""
        response = await self._call_get("crm.lead.get", {"ID": lead_id})
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

        items, _total = await self._paginate("crm.contact.list", params, limit=limit, max_items=limit)
        return items

    async def get_contact(self, contact_id: int) -> Dict[str, Any]:
        """Get contact details."""
        response = await self._call_get("crm.contact.get", {"ID": contact_id})
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

        items, _total = await self._paginate("crm.company.list", params, limit=limit, max_items=limit)
        return items

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

        items, _total = await self._paginate("crm.activity.list", params, limit=limit, max_items=limit)
        return items

    async def get_user(self, user_id: int) -> Dict[str, Any]:
        """Get user details."""
        response = await self._call("user.get", {"ID": user_id})
        if "error" in response:
            return response
        result = response.get("result", [])
        if isinstance(result, list):
            return result[0] if result else {}
        return result or {}

    async def get_users_map(self, ttl_seconds: int = 1800) -> Dict[int, Dict[str, str]]:
        """Return {user_id: {"name": "Фамилия Имя", "position": "..."}}.

        Used to turn raw ASSIGNED_BY_ID / RESPONSIBLE_ID numbers into
        readable manager names. Cached in-memory for ttl_seconds — the
        staff directory changes rarely, no need to hit the API per request.
        """
        import time
        now = time.time()
        if self._users_cache and (now - self._users_cache_ts) < ttl_seconds:
            return self._users_cache

        users: Dict[int, Dict[str, str]] = {}
        start = 0
        while True:
            # No ACTIVE filter — fired managers must still resolve so old
            # leads/deals assigned to them don't show a bare numeric id.
            resp = await self._call("user.get", {}, start=start)
            if isinstance(resp, dict) and "error" in resp:
                break
            batch = resp.get("result", []) if isinstance(resp, dict) else []
            if not isinstance(batch, list) or not batch:
                break
            for u in batch:
                try:
                    uid = int(u.get("ID"))
                except (TypeError, ValueError):
                    continue
                last = (u.get("LAST_NAME") or "").strip()
                first = (u.get("NAME") or "").strip()
                full = " ".join(p for p in (last, first) if p) or f"User {uid}"
                users[uid] = {
                    "name": full,
                    "position": (u.get("WORK_POSITION") or "").strip(),
                }
            next_start = resp.get("next") if isinstance(resp, dict) else None
            if next_start is None or len(batch) < 50:
                break
            start = next_start

        if users:
            self._users_cache = users
            self._users_cache_ts = now
        # On a failed refresh keep serving the stale cache rather than nothing.
        return users or self._users_cache

    async def get_deal_stages(self) -> List[Dict[str, Any]]:
        """Get all deal stages across pipelines via crm.status.list."""
        response = await self._call("crm.status.list", {"filter": {"ENTITY_ID": "DEAL_STAGE"}})
        if "error" in response:
            return []
        result = response.get("result", [])
        return result if isinstance(result, list) else []

    async def find_entity_by_phone(self, phone: str, entity_type: str = "LEAD") -> List[int]:
        """Find lead/deal IDs by phone via crm.duplicate.findbycomm.

        Handles phone-format quirks server-side (+7 / 8 / bare digits).
        entity_type is 'LEAD' or 'DEAL'. Returns [] if nothing matches.
        """
        if not phone:
            return []
        resp = await self._call("crm.duplicate.findbycomm", {
            "type": "PHONE",
            "values": [str(phone)],
            "entity_type": entity_type,
        })
        if isinstance(resp, dict) and "error" in resp:
            return []
        result = resp.get("result") or {}
        ids = result.get(entity_type) or []
        out = []
        for i in ids:
            try:
                out.append(int(i))
            except (TypeError, ValueError):
                continue
        return out

    async def get_deals_by_lead(self, lead_id: int) -> List[Dict[str, Any]]:
        """All deals converted from a given lead (crm.deal.list by LEAD_ID)."""
        items, _total = await self._paginate(
            "crm.deal.list",
            {
                "filter": {"LEAD_ID": lead_id},
                "select": [
                    "ID", "TITLE", "STAGE_ID", "STAGE_SEMANTIC_ID",
                    "OPPORTUNITY", "CATEGORY_ID", "DATE_CREATE", "CLOSED",
                ],
            },
            limit=20,
            max_items=20,
        )
        if isinstance(items, dict) and "error" in items:
            return []
        return items

    async def get_deal_passed_stages(self, deal_id: int) -> set:
        """Set of STAGE_IDs the deal has passed through (crm.stagehistory.list).

        Used to tell whether a deal ever reached a given stage even if it
        has since moved on or fallen out.
        """
        resp = await self._call("crm.stagehistory.list", {
            "entityTypeId": 2,
            "filter": {"OWNER_ID": deal_id},
            "select": ["STAGE_ID"],
        })
        if isinstance(resp, dict) and "error" in resp:
            return set()
        result = resp.get("result") or []
        if isinstance(result, dict):
            result = result.get("items", [])
        return {r.get("STAGE_ID") for r in result if isinstance(r, dict) and r.get("STAGE_ID")}

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

    # ---------- UF metadata + enrichment ----------

    async def get_uf_name_map(self, entity_type_id: int) -> Dict[str, str]:
        """Return {UF_CRM_X: 'Человеческое имя'} for given entity type.

        Uses crm.item.fields with useOriginalUfNames=Y which (unlike
        userfield.list) actually returns titles. Cached for 24 hours
        in memory since field schemas rarely change.
        """
        cached = self._uf_meta_cache.get(entity_type_id)
        if cached and cached["expires"] > datetime.utcnow():
            return cached["map"]

        response = await self._call_get(
            "crm.item.fields",
            {"entityTypeId": entity_type_id, "useOriginalUfNames": "Y"},
        )
        if "error" in response:
            logger.warning("Failed to load UF metadata", entity_type_id=entity_type_id, error=response["error"])
            return {}

        fields = (response.get("result") or {}).get("fields") or {}
        uf_map = {}
        for code, meta in fields.items():
            if not code.startswith("UF_"):
                continue
            title = meta.get("title") or meta.get("formLabel") or meta.get("listLabel") or ""
            if title and title != code:
                uf_map[code] = title

        self._uf_meta_cache[entity_type_id] = {
            "map": uf_map,
            "expires": datetime.utcnow() + timedelta(hours=24),
        }
        logger.info("UF metadata loaded", entity_type_id=entity_type_id, count=len(uf_map))
        return uf_map

    async def enrich_with_uf_names(
        self,
        entity_type_id: int,
        record: Dict[str, Any],
        drop_empty: bool = True,
    ) -> Dict[str, Any]:
        """Rename UF_CRM_* keys to human titles, optionally drop empty UF fields.

        Standard fields (TITLE, STAGE_ID, etc.) pass through unchanged. UF
        fields that have no title in the map keep their technical code so
        nothing is lost.
        """
        if not isinstance(record, dict):
            return record

        uf_map = await self.get_uf_name_map(entity_type_id)
        result = {}
        for key, value in record.items():
            if key.startswith("UF_"):
                if drop_empty and value in (None, "", [], 0, "0", False, "N"):
                    continue
                human = uf_map.get(key)
                if human:
                    result[human] = value
                else:
                    result[key] = value
            else:
                result[key] = value
        return result

    async def get_timeline_comments(
        self,
        entity_type: str,
        entity_id: int,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Get manager comments from a CRM card's timeline.

        entity_type: 'lead' / 'deal' / 'contact' / 'company'.
        Returns list of {AUTHOR_ID, COMMENT, CREATED, FILES?} sorted DESC.
        """
        params = {
            "filter": {
                "ENTITY_TYPE": entity_type,
                "ENTITY_ID": entity_id,
            },
            "order": {"CREATED": "DESC"},
        }
        response = await self._call("crm.timeline.comment.list", params)
        if "error" in response:
            return []
        items = response.get("result", [])
        if not isinstance(items, list):
            return []
        return items[:limit]
