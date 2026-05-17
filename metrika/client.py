"""Yandex Metrika Reporting API client.

Thin wrapper around api-metrika.yandex.net/stat/v1/data. Provides
high-level helpers for the queries Claude tools call (summary, by source,
by UTM campaign, conversion goals).

In-memory 5-min cache by query — same date-range / dimensions hit cache
within 5 min to stay under the 50 req/hr quota.
"""

import aiohttp
import asyncio
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog

from config import settings

logger = structlog.get_logger()


class MetrikaClient:
    """Reporting API wrapper. Methods return ready-to-render dicts."""

    def __init__(self):
        self.token = settings.metrika_oauth_token
        self.counter_id = settings.metrika_counter_id
        self.base_url = settings.metrika_base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        # {cache_key: (expires_ts, data)}
        self._cache: Dict[str, tuple] = {}
        self.cache_ttl_sec = 300  # 5 min

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.counter_id)

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def _query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Raw call to /stat/v1/data with 5-min cache."""
        if not self.enabled:
            return {"error": "Yandex Metrika не настроена (METRIKA_OAUTH_TOKEN/METRIKA_COUNTER_ID пусты)"}

        await self._ensure_session()

        # cache key = sorted params
        key = json.dumps(params, sort_keys=True, ensure_ascii=False)
        now = time.time()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

        params = {**params, "ids": self.counter_id}
        url = f"{self.base_url}/stat/v1/data"

        try:
            async with self._session.get(
                url,
                params=params,
                headers={"Authorization": f"OAuth {self.token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.error("Metrika non-JSON", status=resp.status, body=text[:200])
                    return {"error": f"Metrika non-JSON (status={resp.status})"}

                if resp.status != 200 or "errors" in data or "code" in data:
                    msg = data.get("message") or (data.get("errors") or [{}])[0].get("message") or str(data)[:200]
                    logger.error("Metrika API error", status=resp.status, error=msg)
                    return {"error": f"Metrika API: {msg}"}

                self._cache[key] = (now + self.cache_ttl_sec, data)
                logger.info(
                    "Metrika API call",
                    metrics=params.get("metrics", "")[:60],
                    dimensions=params.get("dimensions", "")[:60],
                    rows=len(data.get("data", [])),
                    sampled=data.get("sampled", False),
                )
                return data
        except Exception as e:
            logger.error("Metrika request failed", error=str(e))
            return {"error": str(e)}

    # ---------- high-level helpers ----------

    async def get_traffic_summary(self, date_from: str, date_to: str) -> Dict[str, Any]:
        """Visits/users/pageviews/bounce/depth/time for a period."""
        data = await self._query({
            "metrics": "ym:s:visits,ym:s:users,ym:s:pageviews,ym:s:bounceRate,ym:s:pageDepth,ym:s:avgVisitDurationSeconds",
            "date1": date_from,
            "date2": date_to,
        })
        if "error" in data:
            return data
        # Metrika totals is a flat list of floats: [visits, users, pageviews, ...]
        totals = data.get("totals") or [0, 0, 0, 0, 0, 0]
        return {
            "date_from": date_from,
            "date_to": date_to,
            "visits": int(totals[0] or 0),
            "users": int(totals[1] or 0),
            "pageviews": int(totals[2] or 0),
            "bounce_rate_pct": round(float(totals[3] or 0), 1),
            "page_depth": round(float(totals[4] or 0), 2),
            "avg_visit_duration_sec": int(totals[5] or 0),
            "sampled": data.get("sampled", False),
        }

    async def get_traffic_by_source(
        self,
        date_from: str,
        date_to: str,
        dimension: str = "ym:s:UTMSource",
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Traffic breakdown by a single dimension.

        dimension options:
          - ym:s:UTMSource / UTMMedium / UTMCampaign / UTMContent / UTMTerm
          - ym:s:trafficSource (canonical channel: direct, search, ad, ...)
          - ym:s:lastTrafficSource
          - ym:s:goal<ID>Conversion (для целей)
        """
        data = await self._query({
            "metrics": "ym:s:visits,ym:s:users,ym:s:bounceRate",
            "dimensions": dimension,
            "date1": date_from,
            "date2": date_to,
            "limit": limit,
            "sort": "-ym:s:visits",
        })
        if "error" in data:
            return data

        rows = []
        for r in data.get("data", []):
            dim_value = (r.get("dimensions") or [{}])[0].get("name") or "(не указано)"
            m = r.get("metrics") or [0, 0, 0]
            rows.append({
                "source": dim_value,
                "visits": int(m[0] or 0),
                "users": int(m[1] or 0),
                "bounce_rate_pct": round(float(m[2] or 0), 1),
            })
        totals = data.get("totals") or [0, 0, 0]
        return {
            "date_from": date_from,
            "date_to": date_to,
            "dimension": dimension,
            "total_visits": int(totals[0] or 0),
            "rows": rows,
            "sampled": data.get("sampled", False),
        }

    async def get_traffic_by_channel(self, date_from: str, date_to: str) -> Dict[str, Any]:
        """Canonical traffic channels (direct/search/ad/social/referral)."""
        return await self.get_traffic_by_source(
            date_from, date_to, dimension="ym:s:<attribution>TrafficSource"
        )

    async def get_goals_list(self) -> Dict[str, Any]:
        """List configured goals in the counter."""
        if not self.enabled:
            return {"error": "Metrika not configured"}
        await self._ensure_session()
        url = f"{self.base_url}/management/v1/counter/{self.counter_id}/goals"
        try:
            async with self._session.get(
                url,
                headers={"Authorization": f"OAuth {self.token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    return {"error": f"Metrika {resp.status}: {data}"}
                goals = []
                for g in data.get("goals", []):
                    goals.append({
                        "id": g.get("id"),
                        "name": g.get("name"),
                        "type": g.get("type"),
                    })
                return {"goals": goals}
        except Exception as e:
            return {"error": str(e)}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# Global instance
metrika_client = MetrikaClient()
