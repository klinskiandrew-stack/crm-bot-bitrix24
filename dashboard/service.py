"""Сервис данных для дашборда трафика.

Мультиканальный: грузит лиды/сделки сразу по нескольким каналам
(ВКонтакте / Авито / Яндекс / Перехват) и тегирует каждую запись
полем `channel`. Фильтрация по каналу + произвольный период — на
клиенте.

Кэш живёт в памяти процесса, обновляется APScheduler-ом каждые N минут.
HTTP-эндпоинты читают только кэш — никаких прямых походов в Bitrix24
из веб-обработчика (это и быстро, и безопасно: один rate limit на всех).
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import structlog
import yaml

from b24.client import Bitrix24Client
from config import settings

logger = structlog.get_logger()

_MSK = timezone(timedelta(hours=3))


# Сколько дней назад считать "актуальными" лидами. 365 дней — даём фронту
# достаточно данных, чтобы крутить период от 7 дней до года без перезапроса.
DEFAULT_LOOKBACK_DAYS = 365

# Сколько лидов/сделок максимум вытащить на канал за обновление.
# Свежие лиды отдают первыми (order_by_date_desc=True), поэтому при
# превышении лимита обрезается старый хвост — это безопаснее, чем
# терять "сегодняшние" лиды как было до фикса.
LEAD_FETCH_LIMIT = 2500
DEAL_FETCH_LIMIT = 1500

# Сколько последних карточек подгружать с таймлайном сразу (остальные —
# по клику в UI через /api/comments/...).
PREFETCH_COMMENTS_FOR_LATEST = 30

# Кастомное поле "Причина отказа" (enumeration) — UF код, постоянный
# для этого Bitrix-портала.
REJECTION_REASON_UF = "UF_CRM_1740994523382"
REJECTION_REASON_DETAIL_UF = "UF_CRM_1723465843"

# Основная воронка ("Автополивы Сделки") = category_id 0.
DEAL_CATEGORY = 0

# STAGE_ID, по которым считаем воронку конверсии. Узнаны через
# crm.status.list(ENTITY_ID=DEAL_STAGE) — см. server probe 25.05.2026.
STAGE_MEASUREMENT_DONE = "UC_BFLJ2N"   # "Замер выполнен"
STAGE_CONTRACT_SIGNED = "PREPARATION"  # "Договор заключен (внесен аванс)"

# Статус лида = квал-лид (Качественный лид, semantic=S).
LEAD_STATUS_QUALIFIED = "CONVERTED"

# Маппинг каналов: (code, label, yaml_key, utm_source_filter).
CHANNEL_DEFS: List[Tuple[str, str, str, Optional[str]]] = [
    ("vk",            "ВКонтакте", "ВКонтакте", None),
    ("avito",         "Авито",     "Авито",     None),
    ("yandex",        "Яндекс",    "Яндекс",    "yandex"),
    ("interception",  "Перехват",  "Перехват",  None),
]


def _load_sources_mapping() -> Dict[str, Any]:
    """Читает sources_mapping.yaml целиком."""
    try:
        path = Path(__file__).resolve().parent.parent / "config" / "sources_mapping.yaml"
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("Failed to load sources_mapping.yaml", error=str(e))
        return {}


def _build_channels() -> List[Dict[str, Any]]:
    """Собирает список каналов из CHANNEL_DEFS + yaml-маппинга."""
    mapping = _load_sources_mapping()
    out = []
    for code, label, yaml_key, utm_src in CHANNEL_DEFS:
        cfg = mapping.get(yaml_key) or {}
        source_ids = [str(x) for x in (cfg.get("bitrix_source_ids") or [])]
        out.append({
            "code": code,
            "label": label,
            "yaml_key": yaml_key,
            "source_ids": source_ids,
            "utm_source": utm_src,
        })
    return out


@dataclass
class CacheState:
    """Снапшот данных дашборда."""

    leads: List[Dict[str, Any]] = field(default_factory=list)
    deals: List[Dict[str, Any]] = field(default_factory=list)
    comments: Dict[str, Dict[int, List[Dict[str, Any]]]] = field(
        default_factory=lambda: {"lead": {}, "deal": {}}
    )
    lead_statuses: Dict[str, str] = field(default_factory=dict)
    deal_stages: Dict[str, str] = field(default_factory=dict)
    users: Dict[int, str] = field(default_factory=dict)
    webforms: Dict[str, str] = field(default_factory=dict)
    source_names: Dict[str, str] = field(default_factory=dict)
    rejection_reasons: Dict[str, str] = field(default_factory=dict)
    # ID сделок, которые когда-либо проходили через эти стадии. Для воронки
    # конверсий: попадание на стадию засчитывается, даже если сделка потом
    # ушла дальше или была провалена.
    measurement_done_deal_ids: set = field(default_factory=set)
    contract_signed_deal_ids: set = field(default_factory=set)
    # Дата FIRST перехода в стадию: {deal_id: ISO datetime string}.
    # Используется для sales-period фильтрации воронки и time-to-sale.
    measurement_done_dates: Dict[int, str] = field(default_factory=dict)
    contract_signed_dates: Dict[int, str] = field(default_factory=dict)
    last_refresh: Optional[datetime] = None
    last_error: Optional[str] = None


class DashboardService:
    """Singleton-сервис: один кэш, одна корутина обновления.

    (Раньше класс назывался VKDashboardService — алиас оставлен ниже.)
    """

    def __init__(self, lookback_days: int = DEFAULT_LOOKBACK_DAYS):
        self.lookback_days = lookback_days
        self.channels = _build_channels()
        self.state = CacheState()
        self._lock = asyncio.Lock()
        self._statuses_expire_at: Optional[datetime] = None

    # ---------- Public API ----------

    def get_snapshot(self) -> Dict[str, Any]:
        """JSON-готовый снапшот для /api/leads."""
        leads = [self._render_lead(l) for l in self.state.leads]
        deals = [self._render_deal(d) for d in self.state.deals]

        return {
            "channels": [
                {"code": c["code"], "label": c["label"], "source_ids": c["source_ids"]}
                for c in self.channels
            ],
            "lookback_days": self.lookback_days,
            "last_refresh": self.state.last_refresh.astimezone(_MSK).isoformat() if self.state.last_refresh else None,
            "last_error": self.state.last_error,
            "stats": self._build_stats(leads, deals),
            "leads": leads,
            "deals": deals,
            "rejection_reasons": self.state.rejection_reasons,
        }

    async def get_comments_for(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        """Возвращает кэш комментариев таймлайна. Если нет — фетчит сейчас."""
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
            await b24.close()

    async def refresh(self) -> None:
        """Полное обновление кэша. Идемпотентно, защищено локом."""
        if self._lock.locked():
            logger.info("Dashboard refresh already running — skipping")
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
                    "Dashboard refresh done",
                    leads=len(self.state.leads),
                    deals=len(self.state.deals),
                    channels=[c["code"] for c in self.channels],
                    seconds=round(duration, 2),
                )
            except Exception as e:
                logger.exception("Dashboard refresh failed")
                self.state.last_error = f"{type(e).__name__}: {e}"
            finally:
                await b24.close()

    # ---------- Internal: data fetching ----------

    async def _fetch_channel_leads(
        self, b24: Bitrix24Client, channel: Dict[str, Any], date_from: str
    ) -> List[Dict[str, Any]]:
        """Лиды одного канала + тег channel-полем. include_full_utm=True даёт
        UTM_CONTENT/UTM_TERM в select."""
        if not channel["source_ids"]:
            return []
        leads = await b24.get_leads(
            assigned_by_ids=[],
            filter_by_date_from=date_from,
            filter_by_source_ids=channel["source_ids"],
            filter_by_utm_source=channel.get("utm_source"),
            include_full_utm=True,
            order_by_date_desc=True,  # свежие первыми, чтобы лимит не отрезал актуал
            limit=LEAD_FETCH_LIMIT,
        )
        if not isinstance(leads, list):
            return []
        for l in leads:
            l["_channel"] = channel["code"]
            l["_channel_label"] = channel["label"]
        return leads

    async def _fetch_channel_deals(
        self, b24: Bitrix24Client, channel: Dict[str, Any], date_from: str
    ) -> List[Dict[str, Any]]:
        if not channel["source_ids"]:
            return []
        deals = await b24.get_deals(
            assigned_by_ids=[],
            filter_by_date_from=date_from,
            filter_by_source_ids=channel["source_ids"],
            filter_by_utm_source=channel.get("utm_source"),
            order_by_date_desc=True,
            limit=DEAL_FETCH_LIMIT,
        )
        if not isinstance(deals, list):
            return []
        for d in deals:
            d["_channel"] = channel["code"]
            d["_channel_label"] = channel["label"]
        return deals

    async def _refresh_inner(self, b24: Bitrix24Client) -> None:
        date_from = (datetime.now(_MSK).date() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")

        # 1. Параллельно по каналам.
        lead_tasks = [self._fetch_channel_leads(b24, c, date_from) for c in self.channels]
        deal_tasks = [self._fetch_channel_deals(b24, c, date_from) for c in self.channels]
        all_results = await asyncio.gather(*lead_tasks, *deal_tasks, return_exceptions=True)
        n = len(self.channels)
        lead_results = all_results[:n]
        deal_results = all_results[n:]

        leads: List[Dict[str, Any]] = []
        deals: List[Dict[str, Any]] = []
        for res in lead_results:
            if isinstance(res, list):
                leads.extend(res)
        for res in deal_results:
            if isinstance(res, list):
                deals.extend(res)

        # Дедуп по ID (на всякий случай).
        seen = set()
        uniq = []
        for l in leads:
            lid = l.get("ID")
            if lid in seen:
                continue
            seen.add(lid)
            uniq.append(l)
        leads = uniq

        seen = set()
        uniq = []
        for d in deals:
            did = d.get("ID")
            if did in seen:
                continue
            seen.add(did)
            uniq.append(d)
        deals = uniq

        # 2. Сортируем по дате создания — свежие сверху.
        leads.sort(key=lambda x: x.get("DATE_CREATE") or "", reverse=True)
        deals.sort(key=lambda x: x.get("DATE_CREATE") or "", reverse=True)

        # 3. Карты статусов/UF (раз в 24 часа).
        if self._statuses_expire_at is None or datetime.now(timezone.utc) > self._statuses_expire_at:
            await self._refresh_status_maps(b24)
            self._statuses_expire_at = datetime.now(timezone.utc) + timedelta(hours=24)

        # 4. Менеджеры.
        await self._refresh_user_names(b24, leads, deals)

        # 4b. Stagehistory — какие сделки прошли через ключевые стадии
        # ("Замер выполнен", "Договор заключён"). Это нужно для воронки
        # конверсий: попадание на стадию — исторический факт, сделка может
        # уже уехать в Монтаж или быть провалена, но её надо посчитать.
        await self._refresh_stage_history(b24)

        # 5. Прелоад комментариев таймлайна для последних N карточек.
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
        """Подтягивает статусы лидов, стадии сделок, источники, формы, enum-значения причин отказа."""
        # Лиды
        resp = await b24._call("crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}})
        if isinstance(resp, dict) and isinstance(resp.get("result"), list):
            self.state.lead_statuses = {
                str(s.get("STATUS_ID")): str(s.get("NAME") or s.get("STATUS_ID"))
                for s in resp["result"]
            }

        # Сделки
        stages = await b24.get_deal_stages()
        if isinstance(stages, list):
            self.state.deal_stages = {
                str(s.get("STATUS_ID")): str(s.get("NAME") or s.get("STATUS_ID"))
                for s in stages
            }

        # Источники (SOURCE_ID → имя)
        src_resp = await b24._call("crm.status.list", {"filter": {"ENTITY_ID": "SOURCE"}})
        if isinstance(src_resp, dict) and isinstance(src_resp.get("result"), list):
            self.state.source_names = {
                str(s.get("STATUS_ID")): str(s.get("NAME") or s.get("STATUS_ID"))
                for s in src_resp["result"]
            }

        # CRM-формы
        wf_resp = await b24._call("crm.webform.list", {})
        if isinstance(wf_resp, dict):
            forms = wf_resp.get("result") or []
            if isinstance(forms, dict):
                forms = forms.get("forms") or []
            if isinstance(forms, list):
                self.state.webforms = {
                    str(f.get("ID") or f.get("id") or ""): str(f.get("NAME") or f.get("name") or "")
                    for f in forms
                    if (f.get("ID") or f.get("id"))
                }

        # Причина отказа — enumeration. crm.item.fields отдаёт items.
        fields_resp = await b24._call_get(
            "crm.item.fields",
            {"entityTypeId": 1, "useOriginalUfNames": "Y"},
        )
        if isinstance(fields_resp, dict):
            all_fields = (fields_resp.get("result") or {}).get("fields") or {}
            reason_meta = all_fields.get(REJECTION_REASON_UF) or {}
            items = reason_meta.get("items") or []
            self.state.rejection_reasons = {
                str(i.get("ID")): str(i.get("VALUE") or i.get("ID"))
                for i in items
                if i.get("ID") is not None
            }

    async def _refresh_user_names(
        self, b24: Bitrix24Client, leads: List[Dict[str, Any]], deals: List[Dict[str, Any]]
    ) -> None:
        """ФИО менеджеров одним батчем."""
        needed_ids = set()
        for r in leads:
            uid = r.get("ASSIGNED_BY_ID")
            if uid:
                needed_ids.add(int(uid))
        for r in deals:
            uid = r.get("ASSIGNED_BY_ID")
            if uid:
                needed_ids.add(int(uid))

        new_ids = [i for i in needed_ids if i not in self.state.users]
        if not new_ids:
            return

        resp = await b24._call("user.get", {"ID": new_ids})
        if isinstance(resp, dict) and isinstance(resp.get("result"), list):
            for u in resp["result"]:
                uid = u.get("ID")
                if not uid:
                    continue
                full = " ".join(filter(None, [u.get("NAME") or "", u.get("LAST_NAME") or ""])).strip()
                self.state.users[int(uid)] = full or f"User #{uid}"

    async def _refresh_stage_history(self, b24: Bitrix24Client) -> None:
        """Грузит ID сделок + дату FIRST перехода через 'Замер выполнен' и 'Договор'.

        Используем crm.stagehistory.list с фильтром по STAGE_ID и периодом
        = `lookback_days`. Это даёт исторические факты — если сделка хоть
        раз была на этой стадии, она засчитывается даже если уехала
        дальше (в Монтаж) или была провалена.

        В отличие от b24.get_stage_history, который обрезает events до 50,
        здесь обходим напрямую через _call, чтобы сохранить CREATED_TIME
        для КАЖДОЙ сделки — это нужно для sales-period фильтрации.
        """
        date_from = (datetime.now(_MSK).date() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        # date_to: завтра, чтобы поймать сегодняшние переходы.
        next_day = (datetime.now(_MSK).date() + timedelta(days=2)).strftime("%Y-%m-%d")

        async def _stage_first_dates(stage_id: str) -> Dict[int, str]:
            """Возвращает {deal_id: ISO timestamp first entry} для всех сделок,
            которые когда-либо переходили в `stage_id` за период lookback."""
            all_events: List[Dict[str, Any]] = []
            start = 0
            params = {
                "entityTypeId": 2,
                "filter": {
                    "=STAGE_ID": stage_id,
                    "=CATEGORY_ID": DEAL_CATEGORY,
                    ">=CREATED_TIME": f"{date_from}T00:00:00+03:00",
                    "<CREATED_TIME": f"{next_day}T00:00:00+03:00",
                },
                "select": ["OWNER_ID", "CREATED_TIME"],
                "order": {"CREATED_TIME": "ASC"},
            }
            SAFETY_CAP = 20_000
            while True:
                resp = await b24._call("crm.stagehistory.list", params, start=start)
                if isinstance(resp, dict) and "error" in resp:
                    logger.warning("stagehistory error", stage=stage_id, error=resp["error"])
                    break
                items = (resp.get("result") or {}).get("items") or []
                if not items:
                    break
                all_events.extend(items)
                next_start = resp.get("next")
                if next_start is None or len(items) < 50:
                    break
                start = next_start
                if len(all_events) >= SAFETY_CAP:
                    logger.warning("stagehistory safety cap hit", stage=stage_id, cap=SAFETY_CAP)
                    break

            # Order ASC → первый встреченный OWNER_ID = самый ранний переход.
            first_dates: Dict[int, str] = {}
            for ev in all_events:
                try:
                    oid = int(ev.get("OWNER_ID") or 0)
                except (TypeError, ValueError):
                    continue
                if oid and oid not in first_dates:
                    first_dates[oid] = ev.get("CREATED_TIME") or ""
            return first_dates

        measurement_dates, contract_dates = await asyncio.gather(
            _stage_first_dates(STAGE_MEASUREMENT_DONE),
            _stage_first_dates(STAGE_CONTRACT_SIGNED),
        )
        self.state.measurement_done_dates = measurement_dates
        self.state.contract_signed_dates = contract_dates
        self.state.measurement_done_deal_ids = set(measurement_dates.keys())
        self.state.contract_signed_deal_ids = set(contract_dates.keys())
        logger.info(
            "Stagehistory loaded",
            measurement_done=len(measurement_dates),
            contract_signed=len(contract_dates),
        )

    # ---------- Rendering ----------

    def _build_card_url(self, kind: str, entity_id: int) -> str:
        """Ссылка на карточку: https://<portal>/crm/lead|deal/details/{id}/"""
        base = settings.b24_portal_url
        return f"{base}/crm/{kind}/details/{entity_id}/"

    def _resolve_rejection(self, value: Any) -> str:
        """UF_CRM_1740994523382 хранится как ID опции. Резолвим в подпись."""
        if value is None or value == "":
            return ""
        return self.state.rejection_reasons.get(str(value), str(value))

    def _render_lead(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        lead_id = int(lead.get("ID") or 0)
        status_id = lead.get("STATUS_ID") or ""
        manager_id = int(lead.get("ASSIGNED_BY_ID")) if lead.get("ASSIGNED_BY_ID") else None
        source_id = str(lead.get("SOURCE_ID") or "")
        webform_id = str(lead.get("WEBFORM_ID") or "")
        return {
            "id": lead_id,
            "title": lead.get("TITLE") or "",
            "name": lead.get("NAME") or "",
            "card_url": self._build_card_url("lead", lead_id),
            "channel": lead.get("_channel") or "",
            "channel_label": lead.get("_channel_label") or "",
            "status_id": status_id,
            "status_name": self.state.lead_statuses.get(status_id, status_id),
            "status_semantic": lead.get("STATUS_SEMANTIC_ID") or "",
            "manager": self.state.users.get(manager_id, "") if manager_id else "",
            "created": lead.get("DATE_CREATE") or "",
            "source_id": source_id,
            "source_name": self.state.source_names.get(source_id, source_id),
            "source_description": lead.get("SOURCE_DESCRIPTION") or "",
            "webform_id": webform_id,
            "webform_name": self.state.webforms.get(webform_id, "") if webform_id else "",
            "opportunity": float(lead.get("OPPORTUNITY") or 0),
            "utm_source": lead.get("UTM_SOURCE") or "",
            "utm_medium": lead.get("UTM_MEDIUM") or "",
            "utm_campaign": lead.get("UTM_CAMPAIGN") or "",
            "utm_content": lead.get("UTM_CONTENT") or "",
            "utm_term": lead.get("UTM_TERM") or "",
            "rejection_reason": self._resolve_rejection(lead.get(REJECTION_REASON_UF)),
            "rejection_reason_detail": str(lead.get(REJECTION_REASON_DETAIL_UF) or ""),
            "is_qualified": status_id == LEAD_STATUS_QUALIFIED,
            "card_comment": lead.get("COMMENTS") or "",
            "comments_cached": lead_id in self.state.comments.get("lead", {}),
            "comments_count": len(self.state.comments.get("lead", {}).get(lead_id, [])),
            "comments": self.state.comments.get("lead", {}).get(lead_id, []),
        }

    def _render_deal(self, deal: Dict[str, Any]) -> Dict[str, Any]:
        deal_id = int(deal.get("ID") or 0)
        stage_id = deal.get("STAGE_ID") or ""
        manager_id = int(deal.get("ASSIGNED_BY_ID")) if deal.get("ASSIGNED_BY_ID") else None
        source_id = str(deal.get("SOURCE_ID") or "")
        return {
            "id": deal_id,
            "title": deal.get("TITLE") or "",
            "card_url": self._build_card_url("deal", deal_id),
            "channel": deal.get("_channel") or "",
            "channel_label": deal.get("_channel_label") or "",
            "stage_id": stage_id,
            "stage_name": self.state.deal_stages.get(stage_id, stage_id),
            "stage_semantic": deal.get("STAGE_SEMANTIC_ID") or "",
            "is_won": deal.get("IS_WON") == "Y",
            "is_closed": deal.get("CLOSED") == "Y",
            # Прошла ли сделка через стадии замера/договора (исторически,
            # по crm.stagehistory.list). Используется для воронки CR.
            "passed_measurement": deal_id in self.state.measurement_done_deal_ids,
            "passed_contract": deal_id in self.state.contract_signed_deal_ids,
            # ISO timestamp ПЕРВОГО перехода в стадию (если был).
            # Нужно для sales-period фильтрации и time-to-sale.
            "measurement_done_at": self.state.measurement_done_dates.get(deal_id, ""),
            "contract_signed_at": self.state.contract_signed_dates.get(deal_id, ""),
            "opportunity": float(deal.get("OPPORTUNITY") or 0),
            "currency": deal.get("CURRENCY_ID") or "RUB",
            "manager": self.state.users.get(manager_id, "") if manager_id else "",
            "created": deal.get("DATE_CREATE") or "",
            "source_id": source_id,
            "source_name": self.state.source_names.get(source_id, source_id),
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


# Алиас для обратной совместимости со старым именем.
VKDashboardService = DashboardService


# Глобальный singleton — один кэш на процесс.
_service: Optional[DashboardService] = None


def get_service() -> DashboardService:
    global _service
    if _service is None:
        _service = DashboardService()
    return _service
