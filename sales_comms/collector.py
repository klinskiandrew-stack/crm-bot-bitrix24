"""Коллектор коммуникаций по сделке из Bitrix24 в локальную БД.

Содержит:
  • sync_deal(deal_id)            — главная точка входа; тянет comments +
                                    activity (звонки/задачи/письма);
                                    Open Lines заводятся в openlines.py
                                    отдельным модулем.
  • iter_active_deal_ids(client)  — список ID сделок в активных стадиях
                                    (используется backfill + cron-sync).

Принципы:
  • НЕ повторяет уже выкачанное (UNIQUE (source_type, source_id) + проверка
    last_*_id в deal_sync_state — но IGNORE покрывает дубли всё равно).
  • Звонки с FILES → ставит в очередь Whisper'а (transcription_status='pending'),
    text оставляет пустым; worker дозальёт.
  • Лочит источник в наглядное поле raw_meta как JSON-снапшот — на случай
    если потом захотим вытащить дополнительное поле без второго похода
    в Bitrix.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import structlog

from b24.client import Bitrix24Client
from sales_comms.db import (
    Communication,
    save_sync_state,
    upsert_many,
)
from sales_comms.enrichment import enrich_deal
from sales_comms.openlines import fetch_session_messages

logger = structlog.get_logger()


# crm.activity TYPE_ID → наш source_type. См. также reports/manager_daily.py.
# Заметка: TYPE_ID=6 (CHAT) — это ВНУТРЕННИЙ чат-комментарий менеджера к
# карточке. Не путать с Open Lines сообщениями: те приходят через
# PROVIDER_ID='IMOPENLINES_SESSION' (см. _is_openline_session ниже),
# и реальные тексты к ним берутся отдельным запросом
# imopenlines.session.history.get.
_ACTIVITY_TYPE_MAP = {
    1: ("task", "Встреча"),
    2: ("call", "Звонок"),
    3: ("task", "Задача"),
    4: ("email", "Письмо"),
    6: ("comment", "Внутренний чат"),
}

# Стадии, которые считаем «активными». См. также digest.py и
# get_pipeline_summary. Сделки в WON/LOSE/JUNK пропускаем при backfill —
# но если сделка ушла в LOSE уже после того как мы её отслеживали,
# продолжаем читать (история нужна для отчёта «реактивация»).
_INACTIVE_SEMANTICS = {"S", "F"}   # success / failed


# ---------- утилиты --------------------------------------------------------

def _parse_bitrix_dt(s: Any) -> Optional[datetime]:
    """Bitrix отдаёт даты в ISO с +03:00 (например '2026-05-27T14:30:00+03:00').
    SQLite сохраним как ISO-строку, парсим в datetime чтобы корректно
    сериализовать (и при необходимости считать диффы)."""
    if not s:
        return None
    try:
        # fromisoformat понимает +03:00 начиная с 3.11
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


def _strip_html(text: Optional[str]) -> str:
    """Чистка одновременно HTML (письма) и BB-разметки (комментарии
    карточки, чат-активити). Сохраняем переносы абзацев, схлопываем
    тройные переводы."""
    if not text:
        return ""
    import re
    # HTML
    out = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    out = re.sub(r"</p\s*>", "\n", out, flags=re.IGNORECASE)
    out = re.sub(r"<[^>]+>", "", out)
    # BB-разметка Bitrix: [p], [b], [url=...]..[/url], [color], [size], …
    out = re.sub(
        r"\[url=([^\]]+)\]([^\[]*)\[/url\]",
        r"\2 (\1)",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"\[/?(b|i|u|s|color|size|url|img|code|p|br|quote|spoiler|font|disk)[^\]]*\]",
        "",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _author_name(uid: Any, users_map: Dict[int, Dict[str, str]]) -> Optional[str]:
    try:
        u = users_map.get(int(uid)) if uid else None
        return (u or {}).get("name")
    except (TypeError, ValueError):
        return None


# ---------- разделители по типам активности --------------------------------

def _build_call_comm(deal_id: int, a: Dict[str, Any], users_map: Dict[int, Dict[str, str]]) -> Communication:
    """Звонок (TYPE_ID=2). Если есть прикреплённые FILES — берём первый
    как audio_url (file_id) и ставим transcription_status='pending'.
    Иначе — 'n/a', звонок без записи (например исходящий через мобилу)."""
    files = a.get("FILES") or []
    audio_url = None
    status = "n/a"
    if isinstance(files, list) and files:
        # FILES — список {id, url, name}. Сохраняем id; download_disk_file
        # потом разрезолвит. URL крон-джобе бесполезен (требует сессии).
        first = files[0]
        if isinstance(first, dict) and first.get("id"):
            audio_url = f"disk:{first['id']}"
            status = "pending"

    duration = None
    try:
        s = a.get("START_TIME"); e = a.get("END_TIME")
        if s and e:
            ds = _parse_bitrix_dt(s); de = _parse_bitrix_dt(e)
            if ds and de and de > ds:
                duration = int((de - ds).total_seconds())
    except Exception:
        pass

    direction = None
    raw_dir = a.get("DIRECTION")
    if raw_dir in (1, "1"):
        direction = "in"
    elif raw_dir in (2, "2"):
        direction = "out"

    # Оценка исхода звонка. В Bitrix НЕТ явного «дозвонился/нет» для
    # звонков с мобильного. Но есть косвенные признаки:
    #   - есть запись (FILES) → разговор СОСТОЯЛСЯ (записывают только
    #     соединённые звонки) → outcome="answered"
    #   - нет записи + нулевая/отсутствующая длительность → скорее всего
    #     НЕДОЗВОН → outcome="likely_no_answer"
    #   - нет записи но есть длительность >20 сек → разговор был, просто
    #     без записи (мобильный) → outcome="answered_no_rec"
    has_record = bool(audio_url)
    if has_record:
        outcome = "answered"
    elif duration and duration >= 20:
        outcome = "answered_no_rec"
    else:
        outcome = "likely_no_answer"

    return Communication(
        deal_id=deal_id,
        source_type="call",
        source_id=str(a.get("ID")),
        direction=direction,
        author_id=a.get("RESPONSIBLE_ID"),
        author_name=_author_name(a.get("RESPONSIBLE_ID"), users_map),
        occurred_at=_parse_bitrix_dt(a.get("START_TIME") or a.get("CREATED")),
        subject=a.get("SUBJECT"),
        text=None,                 # дозальёт transcribe worker
        audio_url=audio_url,
        duration_sec=duration,
        transcription_status=status,
        raw_meta={
            "provider": a.get("PROVIDER_ID"),
            "status": a.get("STATUS"),
            "files_count": len(files) if isinstance(files, list) else 0,
            "call_outcome": outcome,
        },
    )


def _is_openline_session(a: Dict[str, Any]) -> bool:
    """Open Lines сессии видны в crm.activity.list как activity с
    PROVIDER_ID='IMOPENLINES_SESSION'. ASSOCIATED_ENTITY_ID = session_id."""
    return (a.get("PROVIDER_ID") or "") == "IMOPENLINES_SESSION"


def _build_openline_session_marker(deal_id: int, a: Dict[str, Any], users_map: Dict[int, Dict[str, str]]) -> Communication:
    """Маркер OL-сессии в БД. text пустой — сами сообщения подтягиваются
    отдельным fetch_session_messages и пишутся как source_type='openline'.
    Этот маркер нужен чтобы знать что сессия для сделки уже видели и не
    запрашивать историю повторно."""
    return Communication(
        deal_id=deal_id,
        source_type="openline_session",
        source_id=str(a.get("ID")),
        direction=None,
        author_id=a.get("RESPONSIBLE_ID"),
        author_name=_author_name(a.get("RESPONSIBLE_ID"), users_map),
        occurred_at=_parse_bitrix_dt(a.get("START_TIME") or a.get("CREATED")),
        subject=a.get("SUBJECT"),  # «Чат открытой линии — "X" (ChatApp Telegram)»
        text=None,
        transcription_status="n/a",
        raw_meta={
            "session_id": a.get("ASSOCIATED_ENTITY_ID"),
            "provider": a.get("PROVIDER_ID"),
            "provider_type": a.get("PROVIDER_TYPE_ID"),
        },
    )


def _build_simple_activity_comm(deal_id: int, a: Dict[str, Any], users_map: Dict[int, Dict[str, str]]) -> Optional[Communication]:
    """Задача / встреча / письмо / OL-сессия / звонок. Возвращает None для
    неизвестных типов — лучше пропустить, чем класть мусор."""
    if _is_openline_session(a):
        return _build_openline_session_marker(deal_id, a, users_map)
    tid_raw = a.get("TYPE_ID")
    try:
        tid = int(tid_raw)
    except (TypeError, ValueError):
        return None
    if tid == 2:
        return _build_call_comm(deal_id, a, users_map)
    entry = _ACTIVITY_TYPE_MAP.get(tid)
    if not entry:
        return None
    source_type, label = entry

    direction = None
    raw_dir = a.get("DIRECTION")
    if raw_dir in (1, "1"):
        direction = "in"
    elif raw_dir in (2, "2"):
        direction = "out"

    text = _strip_html(a.get("DESCRIPTION"))
    return Communication(
        deal_id=deal_id,
        source_type=source_type,
        source_id=str(a.get("ID")),
        direction=direction,
        author_id=a.get("RESPONSIBLE_ID"),
        author_name=_author_name(a.get("RESPONSIBLE_ID"), users_map),
        occurred_at=_parse_bitrix_dt(a.get("CREATED")),
        subject=a.get("SUBJECT") or label,
        text=text or None,
        transcription_status="n/a",
        raw_meta={"type_id": tid, "status": a.get("STATUS"), "completed": a.get("COMPLETED")},
    )


def _build_comment_comm(deal_id: int, c: Dict[str, Any], users_map: Dict[int, Dict[str, str]]) -> Communication:
    """Комментарий из timeline. У него нет DIRECTION (это менеджер пишет
    себе/коллеге)."""
    return Communication(
        deal_id=deal_id,
        source_type="comment",
        source_id=str(c.get("ID")),
        direction=None,
        author_id=c.get("AUTHOR_ID"),
        author_name=_author_name(c.get("AUTHOR_ID"), users_map),
        occurred_at=_parse_bitrix_dt(c.get("CREATED")),
        subject=None,
        text=(c.get("COMMENT") or "").strip() or None,
        transcription_status="n/a",
        raw_meta=None,
    )


# ---------- главная sync-функция ------------------------------------------

@dataclass
class SyncResult:
    deal_id: int
    comments_added: int = 0
    activities_added: int = 0
    calls_queued: int = 0
    contacts_added: int = 0
    files_added: int = 0
    invoices_added: int = 0
    stages_added: int = 0
    error: Optional[str] = None

    def total_added(self) -> int:
        return (
            self.comments_added + self.activities_added
            + self.contacts_added + self.files_added
            + self.invoices_added + self.stages_added
        )


async def sync_deal(
    client: Bitrix24Client,
    deal_id: int,
    *,
    users_map: Optional[Dict[int, Dict[str, str]]] = None,
    deal_meta: Optional[Dict[str, Any]] = None,
    limit_comments: int = 50,
    limit_activities: int = 100,
) -> SyncResult:
    """Подтянуть всё новое по одной сделке.

    Возвращает SyncResult со счётчиками. Не падает — ошибки кладёт в
    SyncResult.error и пишет в deal_sync_state.sync_error.

    deal_meta — опциональный {ID, STAGE_ID, STATUS_SEMANTIC_ID, ...}, чтобы
    сохранить стадию без отдельного запроса. Если не передать, sync пройдёт
    но в deal_sync_state не запишет стадию (deal_stage останется как был).
    """
    res = SyncResult(deal_id=deal_id)
    try:
        if users_map is None:
            users_map = await client.get_users_map()

        # 1) Комментарии. crm.timeline.comment.list уже умеет фильтр по entity.
        comments = await client.get_timeline_comments("deal", deal_id, limit=limit_comments)
        comm_items: List[Communication] = []
        for c in comments or []:
            try:
                comm_items.append(_build_comment_comm(deal_id, c, users_map))
            except Exception as e:
                logger.warning("Failed to build comment communication", deal_id=deal_id, error=str(e))

        # 2) Активности (звонки + задачи + письма). crm.activity.list с
        # фильтром по OWNER_ID/OWNER_TYPE_ID отдаёт всё что прикрепили к
        # карточке. Без TYPE_ID-фильтра, чтобы все типы за один заход.
        act_resp = await client._call("crm.activity.list", {
            "filter": {"OWNER_ID": deal_id, "OWNER_TYPE_ID": 2},
            "select": [
                "ID", "TYPE_ID", "SUBJECT", "DIRECTION", "STATUS",
                "PROVIDER_ID", "PROVIDER_TYPE_ID", "FILES",
                "CREATED", "START_TIME", "END_TIME",
                "RESPONSIBLE_ID", "DESCRIPTION", "COMPLETED",
                # СРАЗ нужно для openline-сессий: тут лежит session_id,
                # по которому потом подтягиваем сообщения через
                # imopenlines.session.history.get
                "ASSOCIATED_ENTITY_ID",
            ],
            "order": {"ID": "DESC"},
        })
        activities = (act_resp or {}).get("result") or []
        act_items: List[Communication] = []
        calls_pending = 0
        ol_session_ids: List[int] = []
        for a in activities[:limit_activities]:
            built = _build_simple_activity_comm(deal_id, a, users_map)
            if built is None:
                continue
            if built.source_type == "call" and built.transcription_status == "pending":
                calls_pending += 1
            if built.source_type == "openline_session":
                sid = (built.raw_meta or {}).get("session_id")
                try:
                    sid_int = int(sid) if sid is not None else None
                except (TypeError, ValueError):
                    sid_int = None
                if sid_int:
                    ol_session_ids.append(sid_int)
            act_items.append(built)

        # 3a) Для каждой OL-сессии — подтянуть сообщения отдельным методом.
        # imopenlines.session.history.get отдаёт всю переписку разом, в
        # среднем 10-30 сообщений на сессию. На активной сделке обычно
        # 1-3 сессии, так что суммарно <100 запросов за backfill.
        ol_messages: List[Communication] = []
        for sid in ol_session_ids:
            try:
                msgs = await fetch_session_messages(
                    client, deal_id, sid, users_map=users_map
                )
                ol_messages.extend(msgs)
            except Exception as e:
                logger.warning(
                    "OL session messages fetch failed",
                    deal_id=deal_id, session_id=sid, error=str(e),
                )

        # 3b) Записываем всё одной транзакцией. upsert_many игнорит дубли
        # по (source_type, source_id), так что повторные sync безопасны.
        added_c = await upsert_many(comm_items)
        added_a = await upsert_many(act_items)
        added_ol = await upsert_many(ol_messages)
        res.comments_added = added_c
        res.activities_added = added_a + added_ol
        res.calls_queued = calls_pending

        # 4) Lv3 enrichment: контакты + файлы + счета + история стадий.
        # Не критично — если упадёт, основной синк уже сохранён в БД.
        try:
            enriched = await enrich_deal(client, deal_id, activities=activities)
            res.contacts_added = enriched.get("contacts", 0)
            res.files_added = enriched.get("files", 0)
            res.invoices_added = enriched.get("invoices", 0)
            res.stages_added = enriched.get("stages", 0)
        except Exception as e:
            logger.warning("enrich_deal failed", deal_id=deal_id, error=str(e))

        # 5) Фиксируем состояние синка. last_*_id берём как MAX(ID) из того,
        # что только что увидели (а не вставили) — иначе после backfill'а
        # инкрементный sync будет тянуть весь список заново.
        last_comment_id = max((int(c.get("ID") or 0) for c in (comments or [])), default=None) or None
        last_activity_id = max((int(a.get("ID") or 0) for a in activities), default=None) or None
        await save_sync_state(
            deal_id,
            last_comment_id=last_comment_id,
            last_activity_id=last_activity_id,
            deal_stage=(deal_meta or {}).get("STAGE_ID"),
            deal_status_semantic=(deal_meta or {}).get("STATUS_SEMANTIC_ID"),
            sync_error=None,
        )

        logger.info(
            "Deal communications synced",
            deal_id=deal_id,
            comments_added=added_c,
            activities_added=added_a,
            calls_queued=calls_pending,
        )
    except Exception as e:
        res.error = str(e)
        logger.error("sync_deal failed", deal_id=deal_id, error=str(e))
        try:
            await save_sync_state(deal_id, sync_error=str(e)[:500])
        except Exception:
            pass
    return res


# ---------- bulk-варианты для backfill / cron ------------------------------

async def iter_active_deals(client: Bitrix24Client, max_items: int = 500) -> List[Dict[str, Any]]:
    """Активные сделки = filter[CLOSED]=N на стороне Bitrix.

    CLOSED — нативное поле сделки, выставляется в Y когда стадия = WON
    или LOSE (любой воронки). Это надёжнее, чем гадать про STAGE_ID
    суффиксы (которые в дефолтной воронке без префикса :, например
    просто 'WON', а в кастомных C2:WON).
    """
    items, _total = await client._paginate(
        "crm.deal.list",
        params={
            "filter": {"CLOSED": "N"},
            "select": [
                "ID", "TITLE", "STAGE_ID", "STAGE_SEMANTIC_ID",
                "ASSIGNED_BY_ID", "OPPORTUNITY", "DATE_CREATE", "DATE_MODIFY",
                "CATEGORY_ID",
            ],
            "order": {"DATE_MODIFY": "DESC"},
        },
        max_items=max_items,
    )
    if isinstance(items, dict) and items.get("error"):
        logger.error("iter_active_deals fetch failed", error=items.get("error"))
        return []
    # Доп. защита: иногда CLOSED=N но STAGE_SEMANTIC_ID=F (исторический хвост).
    return [d for d in items or [] if (d.get("STAGE_SEMANTIC_ID") or "P").upper() not in _INACTIVE_SEMANTICS]


async def sync_deals_bulk(
    client: Bitrix24Client,
    deal_ids: Iterable[int],
    *,
    deals_meta: Optional[Dict[int, Dict[str, Any]]] = None,
    delay_between: float = 0.3,
) -> Tuple[int, int, int]:
    """Backfill / cron — пробежать по списку сделок последовательно.

    Bitrix limit 2 req/sec; sync_deal делает 2 запроса (comments + activity);
    задержка 0.3с между сделками плюс встроенный rate_limiter не дают
    нам превысить лимит. Возвращает (deals_processed, items_added, queued_calls).
    """
    users_map = await client.get_users_map()
    total_added = 0
    total_calls = 0
    processed = 0
    for did in deal_ids:
        meta = (deals_meta or {}).get(did)
        res = await sync_deal(client, did, users_map=users_map, deal_meta=meta)
        total_added += res.total_added()
        total_calls += res.calls_queued
        processed += 1
        if delay_between:
            await asyncio.sleep(delay_between)
    return processed, total_added, total_calls
