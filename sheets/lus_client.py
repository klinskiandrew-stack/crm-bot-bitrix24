"""Read-only client for the 'ЛУС Growzone' Google Sheet.

Caches the full 'Сделки' worksheet in memory for 10 min to avoid hitting
Sheets API on every question (free quota is 60 reads/min/project).
Parses Russian-formatted money ('р.150 000') and dates ('01.05.2023').
"""

import asyncio
import re
import string
import time
import structlog
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

from config import settings

logger = structlog.get_logger()


# Column letter helper: A..Z, AA..ZZ
_LETTERS = list(string.ascii_uppercase) + [
    a + b for a in string.ascii_uppercase for b in string.ascii_uppercase
]


def _money(s: str) -> Optional[float]:
    """Parse 'р.150 000', 'р.-150,50', '-р.126 530' → float (rubles).
    Returns None for empty/garbage."""
    if not s or not isinstance(s, str):
        return None
    s = s.replace("\xa0", "").replace(" ", "")
    s = s.replace("р.", "").replace("руб", "").replace("₽", "")
    s = s.replace(",", ".")
    if not s or s in ("-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _percent(s: str) -> Optional[float]:
    """Parse '61,97%' → 61.97. Returns None for empty/garbage."""
    if not s or not isinstance(s, str):
        return None
    s = s.replace("\xa0", "").replace(" ", "").replace("%", "").replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date_dmy(s: str) -> Optional[str]:
    """Parse 'DD.MM.YYYY' → 'YYYY-MM-DD'. Returns None for empty/garbage."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    try:
        return datetime.strptime(s, "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


class LusSheetClient:
    """Read 'Сделки' sheet with in-memory cache."""

    SPREADSHEET_ID = "1dVrzQEbIlNJkOr543wUB-cslFFhXw7zrLMieCBFOOmM"
    SHEET_NAME = "Сделки"
    HEADER_ROW = 1  # 0-indexed; row #2 in the sheet is the header
    DATA_START = 2  # 0-indexed; data starts at row #3

    def __init__(self):
        self._gc: Optional[gspread.Client] = None
        self._cache_rows: Optional[List[Dict[str, Any]]] = None
        self._cache_expires: float = 0.0
        self.cache_ttl_sec = 600  # 10 min
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(settings.google_sa_path and Path(settings.google_sa_path).exists())

    def _client(self) -> gspread.Client:
        if self._gc is None:
            creds = Credentials.from_service_account_file(
                settings.google_sa_path,
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets.readonly",
                    "https://www.googleapis.com/auth/drive.readonly",
                ],
            )
            self._gc = gspread.authorize(creds)
        return self._gc

    def _normalize_row(self, headers: List[str], raw: List[str]) -> Dict[str, Any]:
        """Pair raw cells with header names and parse known types."""
        out: Dict[str, Any] = {}
        for i, header in enumerate(headers):
            if not header:
                continue
            value = raw[i] if i < len(raw) else ""
            value = value.strip() if isinstance(value, str) else value
            # Detect type from header name
            h = header.lower()
            if any(k in h for k in ("расход", "выручк", "комисси", "оплачен", "дебитор", "прибыл")):
                out[header] = _money(value)
            elif "%" in header or "рен-т" in h or "% расхода" in h:
                out[header] = _percent(value)
            elif "дата" in h:
                out[header] = _date_dmy(value)
            elif h == "id" or "месяц" in h or "количество" in h or "номер" in h:
                # numeric int when possible
                try:
                    out[header] = int(value) if value not in ("", None) else None
                except (ValueError, TypeError):
                    out[header] = value or None
            else:
                out[header] = value or None
        return out

    async def _load(self) -> List[Dict[str, Any]]:
        """Fetch + normalize sheet. Cached for cache_ttl_sec."""
        async with self._lock:
            now = time.time()
            if self._cache_rows is not None and self._cache_expires > now:
                return self._cache_rows

            def sync_fetch():
                ws = self._client().open_by_key(self.SPREADSHEET_ID).worksheet(self.SHEET_NAME)
                return ws.get_all_values()

            raw_rows = await asyncio.to_thread(sync_fetch)
            if len(raw_rows) < self.DATA_START + 1:
                self._cache_rows = []
                self._cache_expires = now + self.cache_ttl_sec
                return self._cache_rows

            headers = [h.strip() for h in raw_rows[self.HEADER_ROW]]
            data_rows = raw_rows[self.DATA_START:]
            normalized: List[Dict[str, Any]] = []
            for raw in data_rows:
                if not any(c.strip() for c in raw if isinstance(c, str)):
                    continue  # skip empty rows
                row = self._normalize_row(headers, raw)
                if row.get("ID") or row.get("Контрагент"):
                    normalized.append(row)

            self._cache_rows = normalized
            self._cache_expires = now + self.cache_ttl_sec
            logger.info(
                "LUS sheet loaded",
                rows=len(normalized),
                cache_ttl_sec=self.cache_ttl_sec,
            )
            return self._cache_rows

    # ---------- public API ----------

    def card_url(self, row_id: int) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.SPREADSHEET_ID}/edit"

    async def get_deal(self, deal_id: int) -> Optional[Dict[str, Any]]:
        rows = await self._load()
        for r in rows:
            if r.get("ID") == int(deal_id):
                return r
        return None

    async def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Substring match against Контрагент and Номер договора."""
        q = (query or "").strip().lower()
        if not q:
            return []
        rows = await self._load()
        out = []
        for r in rows:
            haystack = " ".join(
                str(r.get(k, "") or "")
                for k in ("Контрагент", "Номер договора", "Город")
            ).lower()
            if q in haystack:
                out.append(r)
                if len(out) >= limit:
                    break
        return out

    async def financials(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        group_by: Optional[str] = None,
        only_completed: bool = False,
    ) -> Dict[str, Any]:
        """Aggregate financial KPIs over a date range, optionally grouped.

        date_from/date_to filter on 'Дата продажи' (YYYY-MM-DD).
        group_by: 'Источник клиента' | 'Направление' | 'Услуга' | 'Партнер' | 'Месяц' | None
        only_completed=True restricts to Статус='Завершен' (revenue recognised).
        """
        rows = await self._load()

        def in_range(r):
            d = r.get("Дата продажи")
            if date_from and (not d or d < date_from):
                return False
            if date_to and (not d or d > date_to):
                return False
            return True

        def is_completed(r):
            return (r.get("Статус") or "").strip().lower() == "завершен"

        filtered = [r for r in rows if in_range(r) and (not only_completed or is_completed(r))]

        def _sum(group, key):
            """Sum only numeric values; ignore strings/None — sheet sometimes
            has stray '#REF!', '—' or text in money cells that _money returns
            as None but type-promoting cells via formulas can yield str too."""
            total = 0.0
            for r in group:
                v = r.get(key)
                if isinstance(v, (int, float)):
                    total += float(v)
            return round(total, 2)

        def aggregate(group: List[Dict[str, Any]]):
            return {
                "count": len(group),
                "revenue_plan": _sum(group, "Выручка план"),
                "revenue_fact": _sum(group, "Выручка признанная"),
                "paid_by_client": _sum(group, "Оплачено клиентом"),
                "debt": _sum(group, "Дебеторка"),
                "expenses_fact": _sum(group, "Расходы итого факт"),
                "margin": _sum(group, "Маржинальная прибыль"),
                "completed_count": sum(1 for r in group if is_completed(r)),
            }

        result: Dict[str, Any] = {
            "date_from": date_from or "all",
            "date_to": date_to or "all",
            "only_completed": only_completed,
            "total_rows": len(rows),
            "in_period_rows": len(filtered),
            "overall": aggregate(filtered),
        }

        if group_by:
            buckets: Dict[str, List[Dict[str, Any]]] = {}
            for r in filtered:
                key = str(r.get(group_by) or "(пусто)")
                buckets.setdefault(key, []).append(r)
            grouped = [{"key": k, **aggregate(v)} for k, v in buckets.items()]
            grouped.sort(key=lambda x: x.get("revenue_fact", 0) or 0, reverse=True)
            result["group_by"] = group_by
            # Cap to 15 rows to keep LLM payload small.
            result["groups"] = grouped[:15]
        return result

    async def status_counts(self) -> Dict[str, int]:
        rows = await self._load()
        counts: Dict[str, int] = {}
        for r in rows:
            s = str(r.get("Статус") or "(пусто)").strip()
            counts[s] = counts.get(s, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: -x[1]))


# Global instance
lus_client = LusSheetClient()
