"""Avito API client (client_credentials flow).

Авито использует "Приложение персональной авторизации" — это OAuth2
client_credentials grant. Access token живёт 24 часа, refresh не нужен:
просто запрашиваем новый по client_id+client_secret.

Endpoints used:
  POST /token                                  → get access_token
  GET  /core/v1/accounts/self                  → profile info
  GET  /core/v1/accounts/{id}/balance/         → счёт (real + bonus)
  POST /core/v1/accounts/operations_history/   → расходы (CPA, тарифы)
  GET  /core/v1/items                          → список объявлений
  POST /stats/v1/accounts/{id}/items           → views/contacts/favorites по объявлениям
  POST /calltracking/v1/getCalls/              → звонки
"""

import aiohttp
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog
from config import settings

logger = structlog.get_logger()


class AvitoClient:
    """Avito API wrapper (client_credentials flow)."""

    def __init__(self):
        self.client_id = settings.avito_client_id
        self.client_secret = settings.avito_client_secret
        self.base_url = "https://api.avito.ru"
        self._session: Optional[aiohttp.ClientSession] = None

        # Access token cached in-memory (24h TTL from Avito)
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

        # Response cache: {key: (expires_ts, data)}
        self._cache: Dict[str, tuple] = {}
        self.cache_ttl_sec = 1800  # 30 min

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def _get_access_token(self) -> str:
        """Get cached or fresh access token (client_credentials flow)."""
        now = time.time()
        if self._access_token and self._token_expires_at > now + 60:
            return self._access_token

        await self._ensure_session()
        try:
            async with self._session.post(
                f"{self.base_url}/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if resp.status != 200 or "access_token" not in data:
                    logger.error("Avito token request failed", status=resp.status, body=data)
                    raise ValueError(f"Avito token error: {data}")

                self._access_token = data["access_token"]
                self._token_expires_at = now + int(data.get("expires_in", 86400))
                logger.info("Avito token obtained", expires_in=data.get("expires_in"))
                return self._access_token
        except Exception as e:
            logger.error("Avito token request exception", error=str(e))
            raise

    async def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """HTTP request with auto-token. Retries once on 401/403."""
        await self._ensure_session()

        for attempt in range(2):
            token = await self._get_access_token()
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {token}"
            headers.setdefault("Content-Type", "application/json")

            try:
                async with self._session.request(
                    method,
                    f"{self.base_url}{endpoint}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                    **kwargs,
                ) as resp:
                    text = await resp.text()

                    if resp.status in (401, 403) and attempt == 0:
                        logger.warning("Avito auth error, refreshing token", status=resp.status)
                        self._token_expires_at = 0
                        continue

                    try:
                        data = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        logger.error("Avito non-JSON response", status=resp.status, body=text[:200])
                        return {"error": f"Avito non-JSON (status={resp.status})"}

                    if resp.status != 200:
                        msg = data.get("message") or data.get("error") or str(data)[:200]
                        logger.error("Avito API error", endpoint=endpoint, status=resp.status, error=msg)
                        return {"error": f"Avito {resp.status}: {msg}"}

                    return data

            except Exception as e:
                logger.error("Avito request exception", endpoint=endpoint, error=str(e))
                return {"error": str(e)}

        return {"error": "Avito: failed after retry"}

    def _cache_get(self, key: str) -> Optional[Any]:
        c = self._cache.get(key)
        if c and c[0] > time.time():
            return c[1]
        return None

    def _cache_set(self, key: str, data: Any):
        self._cache[key] = (time.time() + self.cache_ttl_sec, data)

    # ---------- Public methods ----------

    async def get_profile(self) -> Dict[str, Any]:
        """Get current account profile (email, phones, id, name, url)."""
        if not self.enabled:
            return {"error": "Avito not configured"}

        cached = self._cache_get("profile")
        if cached:
            return cached

        data = await self._request("GET", "/core/v1/accounts/self")
        if "error" not in data:
            self._cache_set("profile", data)
        return data

    async def get_balance(self) -> Dict[str, Any]:
        """Balance: real (rub) + bonus."""
        if not self.enabled:
            return {"error": "Avito not configured"}

        user_id = settings.avito_user_id
        if not user_id:
            return {"error": "AVITO_USER_ID not set"}

        return await self._request("GET", f"/core/v1/accounts/{user_id}/balance/")

    async def get_operations_history(
        self,
        date_from: str,
        date_to: str,
    ) -> Dict[str, Any]:
        """Operations history (charges, tariffs, deposits) for period.

        Args:
            date_from, date_to: YYYY-MM-DD
        Returns aggregated by serviceType + raw operations list.
        """
        if not self.enabled:
            return {"error": "Avito not configured"}

        cache_key = f"ops:{date_from}:{date_to}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        payload = {
            "dateTimeFrom": f"{date_from}T00:00:00Z",
            "dateTimeTo": f"{date_to}T23:59:59Z",
        }
        data = await self._request(
            "POST",
            "/core/v1/accounts/operations_history/",
            data=json.dumps(payload),
        )
        if "error" in data:
            return data

        ops = data.get("result", {}).get("operations", [])

        # Aggregate by serviceType (cpa, tariff, etc.) — show net spend
        # operationType variants: "списание средств", "резервирование...", "сторно", "внесение..."
        SPEND_TYPES = {"списание средств", "резервирование средств под услугу"}
        REFUND_TYPES = {"сторно"}
        DEPOSIT_TYPES = {"внесение CPA аванса", "внесение средств"}

        by_service: Dict[str, Dict[str, float]] = {}
        total_spend = 0.0
        total_refund = 0.0
        total_deposit = 0.0

        for op in ops:
            stype = op.get("serviceType") or op.get("serviceName") or "прочее"
            otype = (op.get("operationType") or "").lower()
            amount = float(op.get("amountTotal", 0))

            if stype not in by_service:
                by_service[stype] = {"spend": 0.0, "refund": 0.0, "deposit": 0.0, "count": 0}
            by_service[stype]["count"] += 1

            if any(s in otype for s in ("списан", "резервирован")):
                by_service[stype]["spend"] += amount
                total_spend += amount
            elif "сторно" in otype:
                by_service[stype]["refund"] += amount
                total_refund += amount
            elif "внесен" in otype:
                by_service[stype]["deposit"] += amount
                total_deposit += amount

        return {
            "date_from": date_from,
            "date_to": date_to,
            "total_spend": round(total_spend, 2),
            "total_refund": round(total_refund, 2),
            "net_spend": round(total_spend - total_refund, 2),
            "total_deposit": round(total_deposit, 2),
            "by_service": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in by_service.items()},
            "operations_count": len(ops),
            "operations": ops[:20],
        }

    async def get_items_list(self, per_page: int = 50) -> Dict[str, Any]:
        """List active items (ads)."""
        if not self.enabled:
            return {"error": "Avito not configured"}

        cache_key = f"items:{per_page}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        params = {"page": 1, "per_page": min(per_page, 100), "status": "active"}
        data = await self._request("GET", "/core/v1/items", params=params)
        if "error" in data:
            return data

        items = []
        for r in data.get("resources", []):
            items.append({
                "id": r.get("id"),
                "title": r.get("title"),
                "price": r.get("price"),
                "status": r.get("status"),
                "address": r.get("address"),
                "category": (r.get("category") or {}).get("name"),
                "url": r.get("url"),
            })
        result = {"total": len(items), "items": items}
        self._cache_set(cache_key, result)
        return result

    async def get_items_stats(
        self,
        date_from: str,
        date_to: str,
        item_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Stats by ad: views / uniqViews / uniqContacts / uniqFavorites.

        If item_ids not provided, auto-fetches active ads list first.
        """
        if not self.enabled:
            return {"error": "Avito not configured"}

        user_id = settings.avito_user_id
        if not user_id:
            return {"error": "AVITO_USER_ID not set"}

        cache_key = f"stats:{date_from}:{date_to}:{item_ids}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        # Auto-load item IDs if not given
        if not item_ids:
            items_data = await self.get_items_list(per_page=100)
            if "error" in items_data:
                return items_data
            item_ids = [it["id"] for it in items_data.get("items", [])]
            if not item_ids:
                return {"error": "Нет активных объявлений"}

        # API limit: max 200 item IDs per request
        payload = {
            "dateFrom": date_from,
            "dateTo": date_to,
            "fields": ["views", "uniqViews", "uniqContacts", "uniqFavorites"],
            "itemIds": item_ids[:200],
        }
        data = await self._request(
            "POST",
            f"/stats/v1/accounts/{user_id}/items",
            data=json.dumps(payload),
        )
        if "error" in data:
            return data

        # Aggregate
        items_stats = []
        total_views = total_uniq_views = total_contacts = total_favorites = 0
        for item in data.get("result", {}).get("items", []):
            views = sum(d.get("views", 0) for d in item.get("stats", []))
            uniq_views = sum(d.get("uniqViews", 0) for d in item.get("stats", []))
            contacts = sum(d.get("uniqContacts", 0) for d in item.get("stats", []))
            favorites = sum(d.get("uniqFavorites", 0) for d in item.get("stats", []))

            total_views += views
            total_uniq_views += uniq_views
            total_contacts += contacts
            total_favorites += favorites

            if views or contacts:
                items_stats.append({
                    "item_id": item.get("itemId"),
                    "views": views,
                    "uniq_views": uniq_views,
                    "contacts": contacts,
                    "favorites": favorites,
                })

        # Sort by views desc
        items_stats.sort(key=lambda x: x["views"], reverse=True)

        result = {
            "date_from": date_from,
            "date_to": date_to,
            "total_views": total_views,
            "total_uniq_views": total_uniq_views,
            "total_contacts": total_contacts,
            "total_favorites": total_favorites,
            "items_count": len(items_stats),
            "items": items_stats[:30],  # top 30
        }
        self._cache_set(cache_key, result)
        return result

    async def get_calls(
        self,
        date_from: str,
        date_to: str,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Calls from calltracking (phone calls to ads).

        Args:
            date_from, date_to: YYYY-MM-DD
        """
        if not self.enabled:
            return {"error": "Avito not configured"}

        cache_key = f"calls:{date_from}:{date_to}:{limit}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        payload = {
            "dateTimeFrom": f"{date_from}T00:00:00Z",
            "dateTimeTo": f"{date_to}T23:59:59Z",
            "limit": min(limit, 100),
        }
        data = await self._request(
            "POST",
            "/calltracking/v1/getCalls/",
            data=json.dumps(payload),
        )
        if "error" in data:
            return data

        calls = data.get("calls", [])
        result = {
            "date_from": date_from,
            "date_to": date_to,
            "total_calls": len(calls),
            "calls": calls[:limit],
        }
        self._cache_set(cache_key, result)
        return result

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


avito_client = AvitoClient()
