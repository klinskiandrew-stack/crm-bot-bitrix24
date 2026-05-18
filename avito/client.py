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

Таймзоны:
  Avito API stats считает по сервер-таймзоне Avito (MSK / UTC+3), несмотря
  на то что timezone-параметр игнорируется (тестировали — без эффекта).
  Поэтому даты `dateFrom`/`dateTo` интерпретируются как MSK-сутки.
  Бот должен передавать MSK-даты (что делается через системный промпт
  "сегодняшняя дата"). Для пересчёта UTC→MSK см. _msk_today().
"""

import aiohttp
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from config import settings

MSK_TZ = timezone(timedelta(hours=3))


def msk_today() -> str:
    """Today in Europe/Moscow as YYYY-MM-DD."""
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d")


def msk_date_shift(date_str: str, days: int) -> str:
    """Shift YYYY-MM-DD by N days (MSK calendar)."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")

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

    async def get_items_list(self, max_items: int = 500, status: str = "active") -> Dict[str, Any]:
        """List items (ads) с пагинацией. По умолчанию active.

        Args:
            max_items: hard limit (защита от runaway больших аккаунтов)
            status: 'active' (по умолчанию) | 'removed' | 'blocked' | '' для всех
        """
        if not self.enabled:
            return {"error": "Avito not configured"}

        cache_key = f"items:{status}:{max_items}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        items: List[Dict[str, Any]] = []
        page = 1
        while len(items) < max_items:
            params = {"page": page, "per_page": 100}
            if status:
                params["status"] = status
            data = await self._request("GET", "/core/v1/items", params=params)
            if "error" in data:
                return data
            chunk = data.get("resources", [])
            if not chunk:
                break
            for r in chunk:
                items.append({
                    "id": r.get("id"),
                    "title": r.get("title"),
                    "price": r.get("price"),
                    "status": r.get("status"),
                    "address": r.get("address"),
                    "category": (r.get("category") or {}).get("name"),
                    "url": r.get("url"),
                })
            if len(chunk) < 100:
                break
            page += 1

        result = {"total": len(items), "items": items[:max_items]}
        self._cache_set(cache_key, result)
        return result

    async def _fetch_stats_aggregate(
        self,
        date_from: str,
        date_to: str,
        item_ids: List[int],
    ) -> Dict[str, Any]:
        """Internal: aggregate stats для конкретного списка IDs.

        Возвращает totals + daily breakdown + per-item top list.
        """
        user_id = settings.avito_user_id
        BATCH = 200

        items_stats: List[Dict[str, Any]] = []
        # per-day aggregation across all items
        daily: Dict[str, Dict[str, int]] = {}
        total_views = total_uniq_views = total_contacts = total_favorites = 0

        for i in range(0, len(item_ids), BATCH):
            payload = {
                "dateFrom": date_from,
                "dateTo": date_to,
                "fields": ["views", "uniqViews", "uniqContacts", "uniqFavorites"],
                "itemIds": item_ids[i:i + BATCH],
            }
            data = await self._request(
                "POST",
                f"/stats/v1/accounts/{user_id}/items",
                data=json.dumps(payload),
            )
            if "error" in data:
                return data

            for item in data.get("result", {}).get("items", []):
                s = item.get("stats", [])
                v = u = c = f = 0
                for day in s:
                    dv = day.get("views", 0)
                    du = day.get("uniqViews", 0)
                    dc = day.get("uniqContacts", 0)
                    df = day.get("uniqFavorites", 0)
                    v += dv; u += du; c += dc; f += df
                    date_key = day.get("date")
                    if date_key:
                        bucket = daily.setdefault(date_key, {"views": 0, "uniq_views": 0, "contacts": 0, "favorites": 0})
                        bucket["views"] += dv
                        bucket["uniq_views"] += du
                        bucket["contacts"] += dc
                        bucket["favorites"] += df

                total_views += v; total_uniq_views += u; total_contacts += c; total_favorites += f
                if v or c:
                    items_stats.append({
                        "item_id": item.get("itemId"),
                        "views": v, "uniq_views": u, "contacts": c, "favorites": f,
                    })

        items_stats.sort(key=lambda x: x["views"], reverse=True)
        daily_sorted = [{"date": k, **v} for k, v in sorted(daily.items())]

        return {
            "total_views": total_views,
            "total_uniq_views": total_uniq_views,
            "total_contacts": total_contacts,
            "total_favorites": total_favorites,
            "by_day": daily_sorted,
            "items_with_activity": len(items_stats),
            "top_items": items_stats[:30],
        }

    async def get_items_stats(
        self,
        date_from: str,
        date_to: str,
        item_ids: Optional[List[int]] = None,
        compare_previous: bool = True,
    ) -> Dict[str, Any]:
        """Stats by ad: views / uniqViews / uniqContacts / uniqFavorites.

        Возвращает:
          - totals (views/uniqViews/contacts/favorites) за период
          - by_day: дневной breakdown
          - top_items: топ-30 объявлений по views
          - previous_period: те же totals за предыдущий период равной длины (для сравнения)

        ВАЖНО:
        - Даты dateFrom/dateTo интерпретируются Avito API как сутки MSK (UTC+3).
        - uniqContacts = обращения клиентов (Позвонить + Написать). Это и есть
          "Контакты" в кабинете Avito Pro. Реальный коллтрекинг отключён.
        - Цифры из API считаются ТОЛЬКО по активным сейчас объявлениям и могут
          отличаться от кабинета на ~15-20% (кабинет также включает показы в выдаче).
        """
        if not self.enabled:
            return {"error": "Avito not configured"}

        user_id = settings.avito_user_id
        if not user_id:
            return {"error": "AVITO_USER_ID not set"}

        cache_key = f"stats:{date_from}:{date_to}:{len(item_ids) if item_ids else 'all'}:cmp={compare_previous}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        # Auto-load ALL active item IDs (with pagination)
        if not item_ids:
            items_data = await self.get_items_list(max_items=2000, status="active")
            if "error" in items_data:
                return items_data
            item_ids = [it["id"] for it in items_data.get("items", [])]
            if not item_ids:
                return {"error": "Нет активных объявлений"}

        current = await self._fetch_stats_aggregate(date_from, date_to, item_ids)
        if "error" in current:
            return current

        previous: Optional[Dict[str, Any]] = None
        if compare_previous:
            try:
                d_from = datetime.strptime(date_from, "%Y-%m-%d")
                d_to = datetime.strptime(date_to, "%Y-%m-%d")
                span_days = (d_to - d_from).days + 1
                prev_to = msk_date_shift(date_from, -1)
                prev_from = msk_date_shift(prev_to, -(span_days - 1))
                prev_data = await self._fetch_stats_aggregate(prev_from, prev_to, item_ids)
                if "error" not in prev_data:
                    previous = {
                        "date_from": prev_from,
                        "date_to": prev_to,
                        "total_views": prev_data["total_views"],
                        "total_uniq_views": prev_data["total_uniq_views"],
                        "total_contacts": prev_data["total_contacts"],
                        "total_favorites": prev_data["total_favorites"],
                    }
            except Exception as e:
                logger.warning("avito previous-period compare failed", error=str(e))

        result = {
            "date_from": date_from,
            "date_to": date_to,
            "timezone": "Europe/Moscow (MSK, UTC+3)",
            "items_total": len(item_ids),
            "items_with_activity": current["items_with_activity"],
            "total_views": current["total_views"],
            "total_uniq_views": current["total_uniq_views"],
            "total_contacts": current["total_contacts"],
            "total_favorites": current["total_favorites"],
            "by_day": current["by_day"],
            "top_items": current["top_items"],
            "previous_period": previous,
            "note": (
                "Даты считаются в MSK. uniqContacts = клики 'Позвонить' + 'Написать' "
                "(в кабинете Avito Pro это столбец 'Контакты'). Цифры считаются по "
                "активным сейчас объявлениям — могут отличаться от кабинета на 15-20% "
                "(кабинет шире, включает показы в выдаче и архивные объявления)."
            ),
        }
        self._cache_set(cache_key, result)
        return result

    async def get_calls(
        self,
        date_from: str,
        date_to: str,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Calltracking calls (записи реальных звонков).

        У Growzone коллтрекинг ОТКЛЮЧЁН (платная услуга Avito Pro), поэтому
        endpoint всегда вернёт пустой список. Если нужна метрика обращений —
        используй get_items_stats (uniqContacts = звонки + сообщения).
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
        if not calls:
            result["note"] = (
                "Коллтрекинг (запись реальных звонков) отключён в кабинете Avito Pro — "
                "это отдельный платный сервис. Для метрики обращений используй avito_stats: "
                "uniqContacts = клики 'Позвонить' + 'Написать'."
            )
        self._cache_set(cache_key, result)
        return result

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


avito_client = AvitoClient()
