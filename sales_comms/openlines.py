"""Извлечение переписки из Open Lines (WhatsApp / Telegram / чат на сайте / Max).

Open Lines в Bitrix24 не привязаны напрямую к сделке. Связка идёт через
**activity типа IMOPENLINES_SESSION**, которая прикрепляется к карточке
лида/сделки. В `ASSOCIATED_ENTITY_ID` activity сидит SESSION_ID, по
которому через `imopenlines.session.history.get` достаётся реальная
переписка (text, sender, дата каждого сообщения).

Поэтому коллектор работает так:
  1. collector.py уже видит OL-сессии когда тянет crm.activity.list по
     сделке — кладёт их как source_type='openline_session' (заглушка
     с пустым text).
  2. fetch_session_messages(session_id) идёт за реальной перепиской и
     возвращает список Communication с source_type='openline'.

Сообщения сохраняются с source_id='ol-msg:{msg_id}' (отдельный от
session_id namespace, чтобы UNIQUE-индекс не путал session-указатель и
её сообщения).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog

from b24.client import Bitrix24Client
from sales_comms.db import Communication

logger = structlog.get_logger()


# Bitrix Bb-разметка в сообщениях ботов: [b]…[/b], [url=…]…[/url], переносы.
_BB_TAG_RE = re.compile(r"\[/?(b|i|u|s|color|size|url|img|code)[^\]]*\]", re.IGNORECASE)
_URL_BB_RE = re.compile(r"\[url=([^\]]+)\]([^\[]*)\[/url\]", re.IGNORECASE)


def _clean_bb(text: Optional[str]) -> str:
    """Снять BB-разметку и привести к читаемому plain-text."""
    if not text:
        return ""
    out = _URL_BB_RE.sub(r"\2 (\1)", text)
    out = _BB_TAG_RE.sub("", out)
    return out.strip()


def _parse_dt(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


async def fetch_session_messages(
    client: Bitrix24Client,
    deal_id: int,
    session_id: int,
    *,
    users_map: Optional[Dict[int, Dict[str, str]]] = None,
) -> List[Communication]:
    """Получить сообщения одной OL-сессии и обернуть их в Communication.

    Возвращает пустой список если сессия не найдена / нет доступа /
    пусто. Не падает.

    sender_id трактуется так:
      • '0' — системные сообщения Bitrix («Создан новый чат…») → пропускаем
      • положительный int — Bitrix user (наш менеджер), direction='out'
      • отрицательный или 'chat…' — клиент (анонимный участник OL),
        direction='in'
    """
    resp = await client._call_get(
        "imopenlines.session.history.get",
        {"SESSION_ID": session_id},
    )
    if not isinstance(resp, dict) or resp.get("error"):
        logger.warning(
            "OL session history fetch failed",
            session_id=session_id, error=resp.get("error") if isinstance(resp, dict) else "no response",
        )
        return []

    result = resp.get("result") or {}
    messages = result.get("message") or {}
    if not isinstance(messages, dict):
        return []

    out: List[Communication] = []
    for msg_id, m in messages.items():
        if not isinstance(m, dict):
            continue
        sender_raw = str(m.get("senderid") or "")
        text_raw = m.get("text") or ""

        # Системные служебные сообщения («Создан новый чат», «Сессия
        # закрыта», «Перенаправлено …») — кладём как есть, но помечаем
        # direction=None: иногда они полезны (например, "Сессия
        # завершена клиентом"), но не должны путать дайджест.
        is_system = sender_raw in ("0", "")

        # Менеджер vs клиент. Bitrix-пользователи — целые положительные
        # числа; для клиента OL отдаёт chat-id'шный recipient.
        direction: Optional[str] = None
        author_id: Optional[int] = None
        try:
            uid = int(sender_raw)
            if uid > 0:
                direction = "out"
                author_id = uid
        except (TypeError, ValueError):
            uid = None
        if direction is None and not is_system:
            direction = "in"

        author_name = None
        if author_id and users_map:
            u = users_map.get(author_id)
            author_name = (u or {}).get("name")

        out.append(Communication(
            deal_id=deal_id,
            source_type="openline",
            source_id=f"ol-msg:{msg_id}",
            direction=direction,
            author_id=author_id,
            author_name=author_name,
            occurred_at=_parse_dt(m.get("date")),
            subject=f"session:{session_id}",
            text=_clean_bb(text_raw) or None,
            transcription_status="n/a",
            raw_meta={"session_id": session_id, "chatid": m.get("chatid")},
        ))

    # Сортируем по времени (history.get отдаёт dict, порядок не гарантирован).
    out.sort(key=lambda c: c.occurred_at or datetime.min)
    return out
