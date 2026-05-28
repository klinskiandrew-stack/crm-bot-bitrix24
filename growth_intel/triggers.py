"""Извлечение «триггеров» из коммуникаций по сделке.

Триггер — это конкретный момент в переписке/звонке, который требует
от менеджера действия (или уже должен был). 6 категорий:

  client_ready_to_pay        Клиент попросил счёт/реквизиты, сказал «оплачу»
  client_promised_deadline   Клиент назвал срок («пришлю в среду», «решу
                             до пятницы»)
  manager_promised_action    Менеджер пообещал клиенту сделать к дате
  client_question_unanswered Клиент задал прямой вопрос — ответа нет
  objection_not_handled      Клиент высказал возражение по цене/срокам,
                             менеджер не отработал
  decision_signal            Клиент сказал «беру», «делаем», «договорились»

Извлечение делает DeepSeek по сырой переписке. На входе — коммуникации
из sales_comms.db, на выходе — структурированный JSON, который мы
вставляем в growth_signals. Каждый триггер уникален по
(deal_id, category, detected_at) — повторный прогон не дублирует.

Эвристика: триггеры младше последнего исходящего сообщения менеджера
по той же сделке после момента триггера => satisfied=1 (менеджер уже
среагировал). Иначе остаётся unsatisfied — лежит в digest как «горит».
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import structlog

from config import settings
from db.connection import db
from sales_comms.db import communications_for_deal

logger = structlog.get_logger()


TRIGGER_CATEGORIES = {
    "client_ready_to_pay": "Клиент проявил готовность оплатить (попросил счёт, реквизиты, сказал «оплачу/переведу/готов оплатить»)",
    "client_promised_deadline": "Клиент назвал конкретную дату когда что-то сделает («оплачу в среду», «решу до пятницы», «пришлю документы завтра»)",
    "manager_promised_action": "Менеджер обещал клиенту что-то сделать к конкретной дате («счёт сегодня», «КП до завтра», «перезвоню в понедельник»)",
    "client_question_unanswered": "Клиент задал прямой вопрос и менеджер на него явно не ответил (нет ответного сообщения по сути вопроса)",
    "objection_not_handled": "Клиент высказал возражение (дорого / долго / подумаю / посоветуюсь / в другом месте дешевле), менеджер не отработал — не привёл аргумент, не уточнил, не предложил альтернативу",
    "decision_signal": "Клиент дал явный сигнал готовности к покупке («беру», «делаем», «договорились», «давайте начинать»)",
}

_SEVERITY_BY_CATEGORY = {
    "client_ready_to_pay": "high",
    "decision_signal": "high",
    "manager_promised_action": "medium",
    "client_promised_deadline": "medium",
    "client_question_unanswered": "medium",
    "objection_not_handled": "medium",
}


@dataclass
class Trigger:
    category: str
    detected_at: datetime
    deadline: Optional[datetime]
    evidence: str          # короткая цитата / пересказ
    severity: str          # 'low' | 'medium' | 'high'


def _format_comms_for_llm(comms: List[Dict[str, Any]], cap: int = 8000) -> str:
    """Превратить коммуникации в компактный текст для DeepSeek."""
    lines = []
    # Идём от старого к новому — LLM лучше «понимает» хронологию слева-направо.
    for c in reversed(comms):
        ts = c.get("occurred_at") or ""
        if isinstance(ts, str) and "T" in ts:
            ts = ts.replace("T", " ")[:16]
        elif isinstance(ts, str) and " " in ts:
            ts = ts[:16]
        src = c.get("source_type", "?")
        direction = c.get("direction") or ""
        author = c.get("author_name") or ("клиент" if direction == "in" else "?")
        text = (c.get("text") or "").strip()
        if not text:
            # Звонок без расшифровки или маркер — добавим минималку, чтобы
            # LLM не потерял временную шкалу. Дешевле чем оставить пробел.
            if src == "call":
                dur = c.get("duration_sec") or 0
                lines.append(f"[{ts}] {author} → ЗВОНОК {dur}с (без расшифровки)")
            continue
        if src == "call":
            lines.append(f"[{ts}] {author} ЗВОНОК (расшифровка): {text[:800]}")
        elif src == "openline":
            who = "МЕНЕДЖЕР" if direction == "out" else "КЛИЕНТ"
            lines.append(f"[{ts}] {who} ({author}): {text[:400]}")
        elif src == "comment":
            lines.append(f"[{ts}] МЕНЕДЖЕР коммент: {text[:300]}")
        elif src in ("task", "email"):
            sub = c.get("subject") or src
            lines.append(f"[{ts}] {src.upper()} «{sub}»: {text[:300]}")
        else:
            lines.append(f"[{ts}] {src}: {text[:300]}")
    joined = "\n".join(lines)
    return joined[:cap] if len(joined) > cap else joined


_SYSTEM_PROMPT = """Ты — аналитик отдела продаж компании Growzone
(благоустройство, автополив, газон). Тебе дают сырой лог коммуникаций
по одной сделке (звонки, чаты в мессенджерах, комментарии менеджеров,
письма). Хронология — сверху старое, снизу свежее.

Найди в этом логе СОБЫТИЯ-ТРИГГЕРЫ, которые требуют (или требовали)
действия менеджера. Категории:

""" + "\n".join(f"  • {k}: {v}" for k, v in TRIGGER_CATEGORIES.items()) + """

Правила:
1. Каждое событие — отдельный триггер. Если клиент попросил счёт И
   назвал дату оплаты, это ДВА триггера.
2. Не выдумывай. Если в логе нет явного сигнала из категории —
   не возвращай ничего по этой категории.
3. evidence — короткая прямая цитата (≤120 символов) либо пересказ
   ОДНОЙ фразой. Без вступлений.
4. detected_at — дата события в формате YYYY-MM-DD (точное время не
   нужно, бери из таймстампа сообщения).
5. deadline — если клиент назвал конкретную дату («оплачу в среду»),
   укажи её в YYYY-MM-DD. Если нет конкретики — null.

ВЕРНИ строго JSON-массив:
[
  {"category": "...", "detected_at": "YYYY-MM-DD",
   "deadline": "YYYY-MM-DD" | null,
   "evidence": "..."}
]

Если триггеров НЕТ — верни пустой массив []."""


async def _call_deepseek(comms_text: str) -> List[Dict[str, Any]]:
    """Один JSON-вызов в DeepSeek. Возвращает список dict'ов или []."""
    if not settings.deepseek_api_key:
        return []
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "Лог коммуникаций по сделке:\n\n" + comms_text},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.1,
        "max_tokens": 1500,
    }
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
            async with s.post(url, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning("Triggers DeepSeek error", status=resp.status, body=text[:200])
                    return []
                data = json.loads(text)
        content = data["choices"][0]["message"]["content"].strip()
        parsed = json.loads(content)
        # DeepSeek с response_format=json_object иногда оборачивает массив
        # в объект {"triggers": [...]} — нормализуем оба варианта.
        if isinstance(parsed, dict):
            for key in ("triggers", "items", "result", "data"):
                if isinstance(parsed.get(key), list):
                    return parsed[key]
            return []
        return parsed if isinstance(parsed, list) else []
    except Exception as e:
        logger.warning("Triggers parse failed", error=str(e))
        return []


def _parse_date(s: Any) -> Optional[datetime]:
    if not s or s in ("null", "none", "None"):
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        # На случай YYYY-MM-DD
        try:
            return datetime.strptime(str(s), "%Y-%m-%d")
        except ValueError:
            return None


def _normalize(raw: Dict[str, Any]) -> Optional[Trigger]:
    cat = str(raw.get("category") or "").strip()
    if cat not in TRIGGER_CATEGORIES:
        return None
    detected = _parse_date(raw.get("detected_at")) or datetime.now()
    deadline = _parse_date(raw.get("deadline"))
    evidence = str(raw.get("evidence") or "").strip()[:500]
    if not evidence:
        return None
    return Trigger(
        category=cat,
        detected_at=detected,
        deadline=deadline,
        evidence=evidence,
        severity=_SEVERITY_BY_CATEGORY.get(cat, "medium"),
    )


# ---------- запись + апдейт «satisfied» -----------------------------------

async def _save_triggers(
    deal_id: int,
    manager_id: Optional[int],
    value_at_risk: Optional[float],
    triggers: List[Trigger],
) -> int:
    """Upsert триггеры в growth_signals. Возвращает сколько новых добавлено."""
    added = 0
    for t in triggers:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO growth_signals
            (deal_id, category, detected_at, deadline, evidence,
             value_at_risk, manager_id, severity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deal_id,
                t.category,
                t.detected_at.isoformat(),
                t.deadline.isoformat() if t.deadline else None,
                t.evidence,
                value_at_risk,
                manager_id,
                t.severity,
            ),
        )
        if cursor.rowcount and cursor.rowcount > 0:
            added += 1
    await db.commit()
    return added


async def _mark_satisfied_by_followup(deal_id: int) -> int:
    """Триггеры считаем satisfied, если ПОСЛЕ detected_at в коммуникациях
    есть исходящее действие менеджера: коммент, исходящее OL-сообщение,
    исходящий звонок. Это эвристика — для отчёта «горит» этого достаточно.
    Возвращает число обновлённых строк."""
    cursor = await db.execute(
        """
        UPDATE growth_signals
        SET satisfied = 1,
            satisfied_at = (
                SELECT MIN(occurred_at) FROM deal_communications
                WHERE deal_id = growth_signals.deal_id
                  AND occurred_at > growth_signals.detected_at
                  AND (direction = 'out' OR source_type IN ('comment', 'task'))
            ),
            satisfied_note = 'auto: detected follow-up activity'
        WHERE deal_id = ?
          AND satisfied = 0
          AND EXISTS (
            SELECT 1 FROM deal_communications
            WHERE deal_id = growth_signals.deal_id
              AND occurred_at > growth_signals.detected_at
              AND (direction = 'out' OR source_type IN ('comment', 'task'))
          )
        """,
        (deal_id,),
    )
    rc = cursor.rowcount or 0
    await db.commit()
    return rc


# ---------- публичная точка входа -----------------------------------------

async def analyze_deal(
    deal_id: int,
    *,
    manager_id: Optional[int] = None,
    opportunity: Optional[float] = None,
    lookback_days: int = 30,
) -> Dict[str, Any]:
    """Пробежать триггерным анализом по одной сделке. Возвращает счётчики.

    Безопасно повторно — UNIQUE по (deal_id, category, detected_at)
    защищает от дублей, satisfied обновляется отдельным проходом.
    """
    comms = await communications_for_deal(deal_id, max_items=40)
    if not comms:
        return {"deal_id": deal_id, "triggers_added": 0, "satisfied_now": 0, "skipped": "no_comms"}

    # Отсечь старьё: триггеры месячной давности уже не actionable.
    cutoff = datetime.now() - timedelta(days=lookback_days)
    fresh = [
        c for c in comms
        if (_parse_date(c.get("occurred_at")) or datetime.min) >= cutoff
    ]
    if not fresh:
        return {"deal_id": deal_id, "triggers_added": 0, "satisfied_now": 0, "skipped": "all_old"}

    comms_text = _format_comms_for_llm(fresh)
    raw = await _call_deepseek(comms_text)
    triggers: List[Trigger] = []
    for item in raw:
        if isinstance(item, dict):
            t = _normalize(item)
            if t:
                triggers.append(t)

    added = await _save_triggers(deal_id, manager_id, opportunity, triggers) if triggers else 0
    satisfied_now = await _mark_satisfied_by_followup(deal_id)

    logger.info(
        "Deal triggers analyzed",
        deal_id=deal_id,
        comms=len(fresh),
        triggers_found=len(triggers),
        triggers_added=added,
        satisfied_now=satisfied_now,
    )
    return {
        "deal_id": deal_id,
        "comms_analyzed": len(fresh),
        "triggers_found": len(triggers),
        "triggers_added": added,
        "satisfied_now": satisfied_now,
    }
