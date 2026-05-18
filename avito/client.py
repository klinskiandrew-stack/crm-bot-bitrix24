"""Avito Ads API client for statistics and campaign data.

Handles OAuth 2.0 flow and provides methods to fetch:
- Campaign list and details
- Statistics by items (views, contacts, calls)
- Promotion bids and performance

In-memory cache (30-60 min TTL) for stats queries.
Refresh token stored in .env, auto-refresh on 403 Forbidden.
"""

import aiohttp
import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import structlog
from config import settings

logger = structlog.get_logger()


class AvitoClient:
    """Avito Ads API wrapper. Methods return ready-to-render dicts."""

    def __init__(self):
        self.client_id = settings.avito_client_id
        self.client_secret = settings.avito_client_secret
        self.access_token = None
        self.refresh_token = settings.avito_refresh_token
        self.token_expires_at = 0
        self.base_url = "https://api.avito.ru"
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, tuple] = {}
        self.cache_ttl_sec = 1800  # 30 min

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def _ensure_token(self):
        """Refresh token if expired."""
        now = time.time()
        if self.access_token and self.token_expires_at > now:
            return

        if not self.refresh_token:
            logger.error("Avito refresh_token not set in .env")
            raise ValueError("AVITO_REFRESH_TOKEN required")

        await self._refresh_access_token()

    async def _refresh_access_token(self):
        """OAuth 2.0 refresh token flow."""
        await self._ensure_session()
        url = f"{self.base_url}/oauth/token"

        try:
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            }

            async with self._session.post(
                url,
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp_data = await resp.json()

                if resp.status != 200:
                    logger.error("Avito token refresh failed", status=resp.status, error=resp_data)
                    raise ValueError(f"Token refresh failed: {resp_data}")

                self.access_token = resp_data.get("access_token")
                self.refresh_token = resp_data.get("refresh_token", self.refresh_token)
                expires_in = resp_data.get("expires_in", 86400)
                self.token_expires_at = time.time() + expires_in

                logger.info(
                    "Avito token refreshed",
                    expires_in=expires_in,
                    token_expires_at=datetime.fromtimestamp(self.token_expires_at),
                )
        except Exception as e:
            logger.error("Avito token refresh request failed", error=str(e))
            raise

    async def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Base HTTP request with OAuth and error handling."""
        await self._ensure_session()
        await self._ensure_token()

        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
                **kwargs,
            ) as resp:
                text = await resp.text()

                if resp.status == 403:
                    logger.warning("Avito 403 — token likely expired, refreshing")
                    self.token_expires_at = 0  # Force refresh
                    await self._ensure_token()
                    return await self._request(method, endpoint, **kwargs)  # Retry

                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    logger.error("Avito non-JSON response", status=resp.status, body=text[:200])
                    return {"error": f"Avito non-JSON (status={resp.status})"}

                if resp.status != 200:
                    msg = data.get("message") or data.get("error") or str(data)[:200]
                    logger.error("Avito API error", status=resp.status, error=msg)
                    return {"error": f"Avito API: {msg}"}

                return data
        except Exception as e:
            logger.error("Avito request failed", error=str(e))
            return {"error": str(e)}

    # ---------- Public API Methods ----------

    async def get_campaigns(self, user_id: str) -> Dict[str, Any]:
        """Get list of active campaigns for a user/account."""
        if not self.enabled:
            return {"error": "Avito API not configured (AVITO_CLIENT_ID/AVITO_CLIENT_SECRET empty)"}

        data = await self._request(
            "GET",
            f"/core/v1/accounts/{user_id}/campaigns",
        )

        if "error" in data:
            return data

        campaigns = []
        for c in data.get("campaigns", []):
            campaigns.append({
                "id": c.get("id"),
                "title": c.get("title"),
                "status": c.get("status"),
                "type": c.get("type"),
                "created_at": c.get("created_at"),
            })

        return {
            "user_id": user_id,
            "total": len(campaigns),
            "campaigns": campaigns,
        }

    async def get_stats_items(
        self,
        user_id: str,
        date_from: str,
        date_to: str,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Get statistics by items (ads) — views, contacts, calls.

        Args:
            user_id: Account/user ID in Avito
            date_from: YYYY-MM-DD
            date_to: YYYY-MM-DD
            limit: Max items to return (default 100, max 1000)
        """
        if not self.enabled:
            return {"error": "Avito not configured"}

        # Check cache
        cache_key = f"stats_items:{user_id}:{date_from}:{date_to}"
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        payload = {
            "dateFrom": date_from,
            "dateTo": date_to,
            "limit": limit,
        }

        data = await self._request(
            "POST",
            f"/core/v1/accounts/{user_id}/stats/items",
            json=payload,
        )

        if "error" in data:
            return data

        items = []
        total_views = 0
        total_contacts = 0
        total_calls = 0

        for item in data.get("items", []):
            stats = item.get("stats", {})
            views = stats.get("views", 0)
            contacts = stats.get("contacts", 0)
            calls = stats.get("calls", 0)

            total_views += views
            total_contacts += contacts
            total_calls += calls

            items.append({
                "item_id": item.get("itemId"),
                "title": item.get("title"),
                "views": views,
                "contacts": contacts,
                "calls": calls,
                "url": item.get("url"),
            })

        result = {
            "user_id": user_id,
            "date_from": date_from,
            "date_to": date_to,
            "total_views": total_views,
            "total_contacts": total_contacts,
            "total_calls": total_calls,
            "items_count": len(items),
            "items": items,
        }

        self._cache[cache_key] = (now + self.cache_ttl_sec, result)
        logger.info("Avito stats fetched", user_id=user_id, items=len(items), views=total_views)

        return result

    async def get_stats_campaigns(
        self,
        user_id: str,
        date_from: str,
        date_to: str,
    ) -> Dict[str, Any]:
        """Get aggregated statistics for campaigns (ads, impressions, cost)."""
        if not self.enabled:
            return {"error": "Avito not configured"}

        cache_key = f"stats_campaigns:{user_id}:{date_from}:{date_to}"
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        payload = {
            "dateFrom": date_from,
            "dateTo": date_to,
        }

        data = await self._request(
            "POST",
            f"/core/v1/accounts/{user_id}/stats/campaigns",
            json=payload,
        )

        if "error" in data:
            return data

        campaigns = []
        total_cost = 0
        total_impressions = 0
        total_clicks = 0

        for c in data.get("campaigns", []):
            stats = c.get("stats", {})
            cost = stats.get("cost", 0)
            impressions = stats.get("impressions", 0)
            clicks = stats.get("clicks", 0)

            total_cost += cost
            total_impressions += impressions
            total_clicks += clicks

            campaigns.append({
                "campaign_id": c.get("campaignId"),
                "title": c.get("title"),
                "cost": cost,
                "impressions": impressions,
                "clicks": clicks,
                "cpc": round(cost / clicks, 2) if clicks > 0 else 0,
                "ctr": round((clicks / impressions) * 100, 2) if impressions > 0 else 0,
            })

        result = {
            "user_id": user_id,
            "date_from": date_from,
            "date_to": date_to,
            "total_cost": total_cost,
            "total_impressions": total_impressions,
            "total_clicks": total_clicks,
            "total_cpc": round(total_cost / total_clicks, 2) if total_clicks > 0 else 0,
            "total_ctr": round((total_clicks / total_impressions) * 100, 2) if total_impressions > 0 else 0,
            "campaigns_count": len(campaigns),
            "campaigns": campaigns,
        }

        self._cache[cache_key] = (now + self.cache_ttl_sec, result)
        logger.info(
            "Avito campaign stats fetched",
            user_id=user_id,
            campaigns=len(campaigns),
            cost=total_cost,
        )

        return result

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# Global instance
avito_client = AvitoClient()
