"""Сервис данных для дашборда трафика.

Идея: фоновое обновление кэша лидов/сделок по фильтру SOURCE_ID канала
(по умолчанию SOURCE_ID="6" — ВКонтакте, см. config/sources_mapping.yaml).

Кэш живёт в памяти процесса, обновляется APScheduler-ом каждые N минут.
HTTP-эндпоинты читают только кэш — никаких прямых походов в Bitrix24
из веб-обработчика (это и быстро, и безопасно: один rate limit на всех).
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
import yaml

from b24.client import Bitrix24Client

logger = structlog.get_logger()

_MSK = timezone(timedelta(hours=3))


# Сколько дней назад считать "актуальными" лидами. 90 дней — компромисс между
# полнотой истории и размером ответа. Меняется через DASHBOARD_LOOKBACK_DAYS.
DEFAULT_LOOKBACK_DAYS = 90

# Сколько лидов максимум вытащить за обновление (страничный лимит).
LEAD_FETCH_LIMIT = 500
DEAL_FETCH_LIMIT = 200

# Сколько последних карточек подгружать с комментариями сразу (остальные —
# по клику в UI через /api/vk-lead/.../comments).
PREFETCH_COMMENTS_FOR_LATEST = 20


def _load_vk_source_ids() -> List[str]:
    """Читает sources_mapping.yaml и возвращает SOURCE_ID канала ВКонтакте.

    Если файл недоступен/изменился — fallback на ['6'].
    """
    try:
        path = Path(__file__).resolve().parent.parent / "config" / "sources_mapping.yaml"
        if not path.exists():
            return ["6"]
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        vk_cfg = data.get("ВКонтакте") or {}
        ids = vk_cfg.get("bitrix_source_ids") or ["6"]
        return [str(x) for x in ids]
    except Exception as e:
        logger.warning("Failed to load VK source_ids from yaml", error=str(e))
        return ["6"]


@dataclass
class CacheState:
    """Снапшот данных дашборда."""

    leads: List[Dict[str, Any]] = field(default_factory=list)
    deals: List[Dict[str, Any]] = field(default_factory=list)
    # {entity_type ("lead"|"deal"): {entity_id: [{author, text, created}]}}
    comments: Dict[str, Dict[int, List[Dict[str, Any]]]] = field(
        default_factory=lambda: {"lead": {}, "deal": {}}
    )
    # Карта STATUS_ID -> человекочитаемое имя (для лидов и стадий сделок)
    lead_statuses: Dict[str, str] = field(default_factory=dict)
    deal_stages: Dict[str, str] = field(default_factory=dict)
    # Карта user_id -> ФИО менеджера
    users: Dict[int, str] = field(default_factory=dict)
    last_refresh: Optional[datetime] = None
    last_error: Optional[str] = None


class VKDashboardService:
    """Singleton-сервис: один кэш, одна корутина обновления."""

    def __init__(self, source_ids: Optional[List[str]] = None, lookback_days: int = DEFAULT_LOOKBACK_DAYS):
        self.source_ids = source_ids or _load_vk_source_ids()
        self.lookback_days = lookback_days
        self.state = CacheState()
        self._lock = asyncio.Lock()
        # Карта статусов — кэш на 24 часа, обновляется при первом refresh.
        self._statuses_expire_at: Optional[datetime] = None

    # ---------- Public API ----------

    def get_snapshot(self) -> Dict[str, Any]:
        """JSON-готовый снапшот для /api/vk-leads."""
        leads = [self._render_lead(l) for l in self.state.leads]
        deals = [self._render_deal(d) for d in self.state.deals]

        return {
            "source_channel": "ВКонтакте",
            "source_ids": self.source_ids,
            "lookback_days": self.lookback_days,
            "last_refresh": self.state.last_refresh.astimezone(_MSK).isoformat() if self.state.last_refresh else None,
            "last_error": self.state.last_error,
            "stats": self._build_stats(leads, deals),
            "leads": leads,
            "deals": deals,
        }

    async def get_comments_for(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        """Возвращает кэш комментариев. Если нет — фетчит сейчас."""
        et = entity_type if entity_type in ("lead", "deal") else "lead"
        cached = self.state.comments.get(et, {}).get(entity_id)
        if cached is not None:
            return cached

        b24 = Bitrix24Client()
        try:
            comments = await b24.get_timeline_comments(et, entity_id, limit=30)
            rendered = self._render_comments(comments)
            self.state.comments[et][entity_id] = rendered
            return rendered
        finally:
            if b24._session and not b24._session.closed:
                await b24._session.close()

    async def refresh(self) -> None:
        """Полное обновление кэша. Идемпотентно, защищено локом."""
        if self._lock.locked():
            logger.info("VK dashboard refresh already running — skipping")
            return

        async with self._lock:
            started = datetime.now(timezone.utc)
            b24 = Bitrix24Client()
            try:
                await self._refresh_inner(b24)
                self.state.last_refresh = datetime.now(timezone.utc)
                self.state.last_error = None
                duration = (datetime.now(timezone.utc) - started).total_seconds()
                logger.info(
                    "VK dashboard refresh done",
                    leads=len(self.state.leads),
                    deals=len(self.state.deals),
                    seconds=round(duration, 2),
                )
            except Exception as e:
                logger.exception("VK dashboard refresh failed")
                self.state.last_error = f"{type(e).__name__}: {e}"
            finally:
                if b24._session and not b24._session.closed:
                    await b24._session.close()

    # ---------- Internal: data fetching ----------

    async def _refresh_inner(self, b24: Bitrix24Client) -> None:
        date_from = (datetime.now(_MSK).date() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")

        # 1. Лиды + сделки параллельно.
        leads_task = b24.get_leads(
            assigned_by_ids=[],
            filter_by_date_from=date_from,
            filter_by_source_ids=self.source_ids,
            limit=LEAD_FETCH_LIMIT,
        )
        deals_task = b24.get_deals(
            assigned_by_ids=[],
            filter_by_date_from=date_from,
            filter_by_source_ids=self.source_ids,
            limit=DEAL_FETCH_LIMIT,
        )
        leads, deals = await asyncio.gather(leads_task, deals_task)

        leads = leads if isinstance(leads, list) else []
        deals = deals if isinstance(deals, list) else []

        # 2. Сортируем по дате создания — свежие сверху.
        leads.sort(key=lambda x: x.get("DATE_CREATE") or "", reverse=True)
        deals.sort(key=lambda x: x.get("DATE_CREATE") or "", reverse=True)

        # 3. Карта статусов/стадий (если устарела или не было).
        if self._statuses_expire_at is None or datetime.now(timezone.utc) > self._statuses_expire_at:
            await self._refresh_status_maps(b24)
            self._statuses_expire_at = datetime.now(timezone.utc) + timedelta(hours=24)

        # 4. Менеджеры (только те, что встречаются в выборке).
        await self._refresh_user_names(b24, leads, deals)

        # 5. Прелоад комментариев для последних 20 карточек.
        new_comments: Dict[str, Dict[int, List[Dict[str, Any]]]] = {"lead": {}, "deal": {}}
        prefetch_leads = leads[:PREFETCH_COMMENTS_FOR_LATEST]
        prefetch_deals = deals[:PREFETCH_COMMENTS_FOR_LATEST]
        comment_tasks = [
            b24.get_timeline_comments("lead", int(l["ID"]), limit=20) for l in prefetch_leads
        ] + [
            b24.get_timeline_comments("deal", int(d["ID"]), limit=20) for d in prefetch_deals
        ]
        if comment_tasks:
            results = await asyncio.gather(*comment_tasks, return_exceptions=True)
            split = len(prefetch_leads)
            for l, res in zip(prefetch_leads, results[:split]):
                if isinstance(res, Exception):
                    continue
                new_comments["lead"][int(l["ID"])] = self._render_comments(res)
            for d, res in zip(prefetch_deals, results[split:]):
                if isinstance(res, Exception):
                    continue
                new_comments["deal"][int(d["ID"])] = self._render_comments(res)

        # 6. Атомарно подменяем кэш.
        self.state.leads = leads
        self.state.deals = deals
        self.state.comments = new_comments

    async def _refresh_status_maps(self, b24: Bitrix24Client) -> None:
        """Подтягивает имена стадий лидов и сделок из Bitrix."""
        # Лиды
        resp = await b24._call("crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}})
        if isinstance(resp, dict) and "result" in resp and isinstance(resp["result"], list):
            self.state.lead_statuses = {
                str(s.get("STATUS_ID")): str(s.get("NAME") or s.get("STATUS_ID"))
                for s in resp["result"]
            }

        # Сделки (все воронки разом)
        stages = await b24.get_deal_stages()
        if isinstance(stages, list):
            self.state.deal_stages = {
                str(s.get("STATUS_ID")): str(s.get("NAME") or s.get("STATUS_ID"))
                for s in stages
            }

    async def _refresh_user_names(
        self, b24: Bitrix24Client, leads: List[Dict[str, Any]], deals: List[Dict[str, Any]]
    ) -> None:
        """Подтягивает ФИО менеджеров (ASSIGNED_BY_ID) одним батчем."""
        needed_ids = set()
        for r in leads:
            uid = r.get("ASSIGNED_BY_ID")
            if uid:
                needed_ids.add(int(uid))
        for r in deals:
            uid = r.get("ASSIGNED_BY_ID")
            if uid:
                needed_ids.add(int(uid))

        # Только новые юзеры (старых имена держим в кэше)
        new_ids = [i for i in needed_ids if i not in self.state.users]
        if not new_ids:
            return

        # user.get умеет фильтр по списку ID
        resp = await b24._call("user.get", {"ID": new_ids})
        if isinstance(resp, dict) and isinstance(resp.get("result"), list):
            for u in resp["result"]:
                uid = u.get("ID")
                if not uid:
                    continue
                full = " ".join(filter(None, [u.get("NAME") or "", u.get("LAST_NAME") or ""])).strip()
                self.state.users[int(uid)] = full or f"User #{uid}"

    # ---------- Rendering ----------

    def _render_lead(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        lead_id = int(lead.get("ID") or 0)
        status_id = lead.get("STATUS_ID") or ""
        manager_id = int(lead.get("ASSIGNED_BY_ID")) if lead.get("ASSIGNED_BY_ID") else None
        return {
            "id": lead_id,
            "title": lead.get("TITLE") or "",
            "name": lead.get("NAME") or "",
            "status_id": status_id,
            "status_name": self.state.lead_statuses.get(status_id, status_id),
            "status_semantic": lead.get("STATUS_SEMANTIC_ID") or "",
            "manager": self.state.users.get(manager_id, "") if manager_id else "",
            "created": lead.get("DATE_CREATE") or "",
            "source_description": lead.get("SOURCE_DESCRIPTION") or "",
            "opportunity": float(lead.get("OPPORTUNITY") or 0),
            "utm_source": lead.get("UTM_SOURCE") or "",
            "utm_medium": lead.get("UTM_MEDIUM") or "",
            "utm_campaign": lead.get("UTM_CAMPAIGN") or "",
            "utm_content": lead.get("UTM_CONTENT") or "",
            "utm_term": lead.get("UTM_TERM") or "",
            "comments_cached": lead_id in self.state.comments.get("lead", {}),
            "comments_count": len(self.state.comments.get("lead", {}).get(lead_id, [])),
            "comments": self.state.comments.get("lead", {}).get(lead_id, []),
        }

    def _render_deal(self, deal: Dict[str, Any]) -> Dict[str, Any]:
        deal_id = int(deal.get("ID") or 0)
        stage_id = deal.get("STAGE_ID") or ""
        manager_id = int(deal.get("ASSIGNED_BY_ID")) if deal.get("ASSIGNED_BY_ID") else None
        return {
            "id": deal_id,
            "title": deal.get("TITLE") or "",
            "stage_id": stage_id,
            "stage_name": self.state.deal_stages.get(stage_id, stage_id),
            "stage_semantic": deal.get("STAGE_SEMANTIC_ID") or "",
            "is_won": deal.get("IS_WON") == "Y",
            "is_closed": deal.get("CLOSED") == "Y",
            "opportunity": float(deal.get("OPPORTUNITY") or 0),
            "currency": deal.get("CURRENCY_ID") or "RUB",
            "manager": self.state.users.get(manager_id, "") if manager_id else "",
            "created": deal.get("DATE_CREATE") or "",
            "source_description": deal.get("SOURCE_DESCRIPTION") or "",
            "utm_source": deal.get("UTM_SOURCE") or "",
            "utm_medium": deal.get("UTM_MEDIUM") or "",
            "utm_campaign": deal.get("UTM_CAMPAIGN") or "",
            "utm_content": deal.get("UTM_CONTENT") or "",
            "utm_term": deal.get("UTM_TERM") or "",
            "comments_cached": deal_id in self.state.comments.get("deal", {}),
            "comments_count": len(self.state.comments.get("deal", {}).get(deal_id, [])),
            "comments": self.state.comments.get("deal", {}).get(deal_id, []),
        }

    def _render_comments(self, raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for c in raw or []:
            author_id = c.get("AUTHOR_ID")
            try:
                author_id_int = int(author_id) if author_id is not None else None
            except (TypeError, ValueError):
                author_id_int = None
            author = self.state.users.get(author_id_int, f"User #{author_id}") if author_id_int else "—"
            text = c.get("COMMENT") or ""
            out.append({
                "author": author,
                "author_id": author_id_int,
                "text": text,
                "created": c.get("CREATED") or "",
            })
        return out

    def _build_stats(self, leads: List[Dict[str, Any]], deals: List[Dict[str, Any]]) -> Dict[str, Any]:
        today_msk = datetime.now(_MSK).date().isoformat()

        def is_today(record: Dict[str, Any]) -> bool:
            return (record.get("created") or "")[:10] == today_msk

        leads_today = sum(1 for l in leads if is_today(l))
        deals_today = sum(1 for d in deals if is_today(d))

        leads_by_status: Dict[str, int] = {}
        for l in leads:
            key = l.get("status_name") or "—"
            leads_by_status[key] = leads_by_status.get(key, 0) + 1

        deals_by_stage: Dict[str, int] = {}
        for d in deals:
            key = d.get("stage_name") or "—"
            deals_by_stage[key] = deals_by_stage.get(key, 0) + 1

        won_deals = [d for d in deals if d.get("is_won")]
        won_sum = sum(d.get("opportunity") or 0 for d in won_deals)

        return {
            "leads_total": len(leads),
            "leads_today": leads_today,
            "deals_total": len(deals),
            "deals_today": deals_today,
            "deals_won": len(won_deals),
            "deals_won_sum": won_sum,
            "leads_by_status": dict(sorted(leads_by_status.items(), key=lambda x: -x[1])),
            "deals_by_stage": dict(sorted(deals_by_stage.items(), key=lambda x: -x[1])),
        }


# Глобальный singleton — один кэш на процесс.
_service: Optional[VKDashboardService] = None


def get_service() -> VKDashboardService:
    global _service
    if _service is None:
        _service = VKDashboardService()
    return _service
