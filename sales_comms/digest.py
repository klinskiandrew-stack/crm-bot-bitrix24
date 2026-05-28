"""Дайджест «состояние сделок» — читает локальную deal_communications,
формирует промпт для DeepSeek, возвращает структурированный отчёт.

Поток (для инструмента deals_status_digest):
  1. По фильтру (manager_name, only_with_recent_activity, days_back)
     находим целевой набор сделок из b24 + дополняем стадией/суммой.
  2. По каждой сделке вытаскиваем последние N коммуникаций из
     deal_communications.
  3. Собираем компактный текст-контекст ≤ ~15K токенов: для каждой
     сделки — заголовок + 5-10 последних реплик в формате
     «27.05 Шеян (out, звонок 3:21): расшифровка…». DeepSeek получает
     указание сгруппировать по приоритету.
  4. Возвращаем уже HTML-форматированный текст для Telegram.

Этот модуль НЕ зовёт Bitrix за переписками — он опирается на то, что
sales_comms_sync уже всё подтянул. Поэтому отчёт строится за секунды,
независимо от объёма CRM.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import structlog

from b24.client import Bitrix24Client
from config import settings
from sales_comms.db import communications_for_deals

logger = structlog.get_logger()


# Лимит контекста для DeepSeek. На каждую сделку аккуратно ~250-400
# токенов (заголовок + 5-10 строк по 30-50 токенов). 50 сделок × 350 =
# 17K — комфортно укладывается в 250K input-cap бота.
_MAX_COMMS_PER_DEAL = 10
_MAX_DEALS_IN_PROMPT = 60
_CALL_TEXT_CAP = 1200       # символов из расшифровки звонка
_TEXT_CAP = 350             # обычная реплика / коммент


# ---------- сбор данных ----------------------------------------------------

async def _fetch_target_deals(
    client: Bitrix24Client,
    *,
    manager_name: Optional[str] = None,
    only_with_recent_activity_days: Optional[int] = None,
    stages: Optional[List[str]] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Достаём из Bitrix целевой набор сделок с минимумом полей."""
    # Используем уже существующий get_deals — он сам обрабатывает
    # manager_name (резолвит ID), фильтр по стадии. Лимит 100 — это
    # верхняя планка одного дайджеста.
    extra: Dict[str, Any] = {"limit": min(limit, 100)}
    if only_with_recent_activity_days and only_with_recent_activity_days > 0:
        cutoff = (datetime.now() - timedelta(days=only_with_recent_activity_days)).strftime("%Y-%m-%d")
        extra["filter_by_date_from"] = cutoff
    # Резолв menager_name → ID-шники делается в ToolHandlers, здесь же
    # мы работаем напрямую — fetch минимально, без manager_name.
    result = await client.get_deals(
        assigned_by_ids=None,
        filter_by_stage=stages[0] if stages and len(stages) == 1 else None,
        limit=extra["limit"],
        filter_by_date_from=extra.get("filter_by_date_from"),
        return_total=True,
    )
    deals = result.get("items") if isinstance(result, dict) else result
    if not isinstance(deals, list):
        deals = []

    # Только активные стадии (skip WON/LOSE/JUNK). См. collector.iter_active_deals.
    active = []
    for d in deals:
        stage = (d.get("STAGE_ID") or "").upper()
        if stage.endswith(":WON") or stage.endswith(":LOSE") or stage.endswith(":LOST"):
            continue
        if stage.endswith(":JUNK") or stage.endswith(":APOLOGY"):
            continue
        active.append(d)
    return active[:limit]


def _format_comm(c: Dict[str, Any]) -> str:
    """Одна строчка коммуникации для промпта DeepSeek."""
    ts = c.get("occurred_at") or "?"
    # Берём только дату и время до минут — час/мин достаточно для
    # «вчера в 14:30», секунды съедают токены.
    if isinstance(ts, str) and "T" in ts:
        ts = ts.replace("T", " ")[:16]
    elif isinstance(ts, str) and " " in ts:
        ts = ts[:16]
    src = c.get("source_type", "?")
    direction = c.get("direction") or "—"
    author = c.get("author_name") or ("клиент" if direction == "in" else "?")
    text = (c.get("text") or "").strip()

    if src == "call":
        # для звонков — длительность и расшифровка (если есть)
        dur = c.get("duration_sec") or 0
        dur_label = f"{dur//60}:{dur%60:02d}" if dur else "?"
        if c.get("transcription_status") == "pending":
            return f"[{ts}] {author} → звонок {dur_label} ({'in' if direction=='in' else 'out'}) — расшифровка в очереди"
        if not text:
            return f"[{ts}] {author} → звонок {dur_label}"
        return f"[{ts}] {author} → звонок {dur_label}: {text[:_CALL_TEXT_CAP]}"
    if src == "comment":
        return f"[{ts}] {author} коммент: {text[:_TEXT_CAP]}"
    if src == "task":
        sub = c.get("subject") or "задача"
        return f"[{ts}] {author} задача «{sub}»: {text[:_TEXT_CAP]}" if text else f"[{ts}] {author} задача «{sub}»"
    if src == "email":
        sub = c.get("subject") or "письмо"
        return f"[{ts}] {author} email «{sub}»: {text[:_TEXT_CAP]}"
    if src == "openline":
        speaker = author if direction == "out" else "клиент"
        return f"[{ts}] {speaker} (чат): {text[:_TEXT_CAP]}"
    if src == "openline_session":
        sub = c.get("subject") or "OL-сессия"
        return f"[{ts}] {sub}"
    # fallback
    return f"[{ts}] {src}: {text[:_TEXT_CAP]}"


async def _build_context(
    client: Bitrix24Client,
    deals: List[Dict[str, Any]],
) -> str:
    """Собрать text-блок для DeepSeek: для каждой сделки — короткая шапка
    и до N последних коммуникаций."""
    deal_ids = [int(d["ID"]) for d in deals if d.get("ID")]
    comms_by_deal = await communications_for_deals(deal_ids, per_deal=_MAX_COMMS_PER_DEAL)

    users_map = await client.get_users_map()
    lines: List[str] = []
    for d in deals[:_MAX_DEALS_IN_PROMPT]:
        did = int(d["ID"])
        title = (d.get("TITLE") or "").strip() or "(без названия)"
        opp = d.get("OPPORTUNITY") or 0
        try:
            opp_f = float(opp)
        except (TypeError, ValueError):
            opp_f = 0.0
        mgr_id = d.get("ASSIGNED_BY_ID")
        mgr_name = (users_map.get(int(mgr_id)) or {}).get("name") if mgr_id else None
        stage = d.get("STAGE_ID") or ""

        # Шапка сделки — компактно. ID нужен LLM, чтобы он мог процитировать
        # в ответе («сделка 18775» — потом РОП легко найдёт).
        header = f"\n## Сделка {did} — {title[:80]}"
        meta = []
        if opp_f:
            meta.append(f"₽{int(opp_f):,}".replace(",", " "))
        if mgr_name:
            meta.append(mgr_name)
        if stage:
            meta.append(f"стадия={stage}")
        if meta:
            header += f"  ({' · '.join(meta)})"
        lines.append(header)

        comms = comms_by_deal.get(did) or []
        if not comms:
            lines.append("  (нет коммуникаций в локальной БД)")
            continue
        for c in comms:
            lines.append("  " + _format_comm(c))

    return "\n".join(lines)


# ---------- вызов LLM -----------------------------------------------------

_SYSTEM_PROMPT = """Ты — аналитик отдела продаж. Тебе дают сырой дамп
коммуникаций по активным сделкам компании Growzone (благоустройство:
автополив, газон, освещение). На каждую сделку: заголовок и последние
10 событий — звонки (с расшифровкой), комментарии менеджеров, задачи,
письма, переписка из мессенджеров.

Твоя задача — выдать сводку для руководителя отдела продаж в формате:

🔴 Срочно (требуют действия СЕГОДНЯ) — N шт
• <сделка ID — клиент, сумма>: что произошло, что ждём и от кого,
  есть ли обещанная дата → <менеджер>

🟡 Ждём от клиента — N шт
• …

🟢 В работе, контакт свежий (≤3 дней) — N шт
• …

⏸️ Без движения >7 дней (кандидаты на реактивацию) — N шт
• …

Правила:
1. «Срочно» = клиент прислал реквизиты / обещал оплатить / спросил
   важный вопрос — и менеджер не ответил больше суток. Или: менеджер
   обещал перезвонить, и срок прошёл.
2. Указывай ID сделки в каждой строчке — РОП по нему быстро найдёт.
3. Не выдумывай факты — если в дампе нет упоминания «реквизитов» или
   «оплаты», не пиши их. Лучше короче, чем додумывать.
4. Russian, Telegram-HTML (<b>...</b>, <i>...</i>). НЕ markdown.
5. Длина ответа ≤ 3500 символов. Если сделок много — фокусируйся на
   срочных, остальное упомяни числом.
6. Если ничего критичного — так и скажи в начале.
"""


async def call_deepseek(context: str) -> str:
    """Прямой вызов DeepSeek (как в lead_reports/call_analyzer.py)."""
    if not settings.deepseek_api_key:
        return "⚠️ DeepSeek API не настроен — дайджест собрать не могу."

    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "Сырые коммуникации по сделкам:\n\n" + context[:80_000]},
        ],
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
        "max_tokens": 2500,
    }
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
            async with s.post(url, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error("Digest DeepSeek error", status=resp.status, body=text[:300])
                    return f"⚠️ DeepSeek вернул {resp.status}: {text[:200]}"
                data = json.loads(text)
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error("Digest LLM call failed", error=str(e))
        return f"⚠️ Не удалось получить ответ от DeepSeek: {e}"


# ---------- публичная функция ---------------------------------------------

async def build_digest(
    *,
    client: Optional[Bitrix24Client] = None,
    manager_name: Optional[str] = None,
    days_back: int = 7,
    limit: int = 80,
) -> Dict[str, Any]:
    """Собрать дайджест по живым сделкам. Возвращает:
        {
          'text':  <HTML-сводка для Telegram>,
          'deals_count': N,
          'comms_count': M,
          'sources': {'comment': X, 'call': Y, ...},
        }
    Не падает: при пустой выборке возвращает понятное сообщение."""
    own_client = client is None
    if own_client:
        client = Bitrix24Client()
    try:
        deals = await _fetch_target_deals(
            client,
            manager_name=manager_name,
            only_with_recent_activity_days=days_back,
            limit=limit,
        )
        if not deals:
            return {
                "text": "📊 <b>Активные сделки</b>\n\nЗа период не нашлось подходящих сделок.",
                "deals_count": 0,
                "comms_count": 0,
                "sources": {},
            }

        context = await _build_context(client, deals)
        # Считаем компанию метрик для логирования.
        comms_count = context.count("\n  [")
        digest_text = await call_deepseek(context)

        header = (
            f"📊 <b>Активные сделки</b> — {len(deals)} шт\n"
            f"<i>На основе локальной базы коммуникаций "
            f"(обновляется каждый час)</i>\n\n"
        )
        return {
            "text": header + digest_text,
            "deals_count": len(deals),
            "comms_count": comms_count,
            "sources": {},
        }
    finally:
        if own_client:
            try:
                await client.close()
            except Exception:
                pass
