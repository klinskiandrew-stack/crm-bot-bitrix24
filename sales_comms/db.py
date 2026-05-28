"""CRUD-обёртки над таблицами deal_communications + deal_sync_state.

Никакой бизнес-логики — только запись/чтение. Бизнес-правила (что
синхронизировать, как форматировать дайджест) живут в collector.py и
digest.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

import structlog

from db.connection import db

logger = structlog.get_logger()


# ---------- shape объектов -------------------------------------------------

@dataclass
class Communication:
    """Одна запись в deal_communications. Используется и для записи (upsert),
    и для чтения (digest). Поля совпадают с колонками таблицы 1:1."""
    deal_id: int
    source_type: str          # 'comment' | 'call' | 'task' | 'email' | 'openline'
    source_id: str
    direction: Optional[str] = None
    author_id: Optional[int] = None
    author_name: Optional[str] = None
    occurred_at: Optional[datetime] = None
    subject: Optional[str] = None
    text: Optional[str] = None
    audio_url: Optional[str] = None
    duration_sec: Optional[int] = None
    transcription_status: Optional[str] = None   # 'pending' | 'done' | 'failed' | 'n/a'
    transcription_error: Optional[str] = None
    raw_meta: Optional[Dict[str, Any]] = None


# ---------- запись ---------------------------------------------------------

async def upsert_many(items: Sequence[Communication]) -> int:
    """Insert OR IGNORE по (source_type, source_id). Возвращает число
    реально добавленных строк. Существующие записи не трогаются — текст
    звонка после транскрипции дописывается отдельным запросом."""
    if not items:
        return 0
    added = 0
    for c in items:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO deal_communications
            (deal_id, source_type, source_id, direction, author_id,
             author_name, occurred_at, subject, text, audio_url,
             duration_sec, transcription_status, raw_meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c.deal_id,
                c.source_type,
                c.source_id,
                c.direction,
                c.author_id,
                c.author_name,
                c.occurred_at.isoformat() if isinstance(c.occurred_at, datetime) else c.occurred_at,
                c.subject,
                c.text,
                c.audio_url,
                c.duration_sec,
                c.transcription_status,
                json.dumps(c.raw_meta, ensure_ascii=False) if c.raw_meta else None,
            ),
        )
        if cursor.rowcount and cursor.rowcount > 0:
            added += 1
    await db.commit()
    return added


async def mark_transcription_done(comm_id: int, text: str) -> None:
    """Звонок расшифровался — записываем текст и закрываем статус."""
    await db.execute(
        """
        UPDATE deal_communications
        SET text = ?, transcription_status = 'done', transcription_error = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (text, comm_id),
    )
    await db.commit()


async def mark_transcription_failed(comm_id: int, err: str) -> None:
    """Whisper упал — фиксируем причину, чтобы не дёргать его снова в цикле."""
    await db.execute(
        """
        UPDATE deal_communications
        SET transcription_status = 'failed', transcription_error = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (err[:500], comm_id),
    )
    await db.commit()


# ---------- чтение ---------------------------------------------------------

async def pending_transcriptions(limit: int = 3) -> List[Dict[str, Any]]:
    """Берём не больше N записей со status='pending' — в порядке появления.
    Whisper долгий (~realtime), 2-3 за пробуждение крон-джоба норм."""
    rows = await db.fetch_all(
        """
        SELECT id, deal_id, source_id, audio_url, duration_sec
        FROM deal_communications
        WHERE transcription_status = 'pending' AND audio_url IS NOT NULL
        ORDER BY occurred_at ASC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in rows or []]


async def communications_for_deal(deal_id: int, max_items: int = 40) -> List[Dict[str, Any]]:
    """Все коммуникации по сделке, новые сначала. Используется в digest."""
    rows = await db.fetch_all(
        """
        SELECT id, source_type, direction, author_name, occurred_at,
               subject, text, duration_sec, transcription_status
        FROM deal_communications
        WHERE deal_id = ?
        ORDER BY occurred_at DESC
        LIMIT ?
        """,
        (deal_id, max_items),
    )
    return [dict(r) for r in rows or []]


async def communications_for_deals(deal_ids: Iterable[int], per_deal: int = 15) -> Dict[int, List[Dict[str, Any]]]:
    """Сводное чтение под дайджест. Возвращает {deal_id: [comm, ...]}.
    SQLite не умеет нативный LIMIT-per-group, поэтому проходим по списку
    в Python — для 50-100 сделок дешевле чем выкручиваться через ROW_NUMBER."""
    out: Dict[int, List[Dict[str, Any]]] = {}
    for did in deal_ids:
        out[did] = await communications_for_deal(did, max_items=per_deal)
    return out


# ---------- sync state -----------------------------------------------------

async def get_sync_state(deal_id: int) -> Optional[Dict[str, Any]]:
    row = await db.fetch_one(
        "SELECT * FROM deal_sync_state WHERE deal_id = ?", (deal_id,)
    )
    return dict(row) if row else None


async def save_sync_state(
    deal_id: int,
    *,
    last_comment_id: Optional[int] = None,
    last_activity_id: Optional[int] = None,
    last_openline_msg_id: Optional[int] = None,
    deal_stage: Optional[str] = None,
    deal_status_semantic: Optional[str] = None,
    sync_error: Optional[str] = None,
) -> None:
    """Idempotent upsert состояния синка. Принимает только заполненные
    поля — остальные остаются как были."""
    existing = await get_sync_state(deal_id)
    if existing is None:
        await db.execute(
            """
            INSERT INTO deal_sync_state
            (deal_id, last_synced_at, last_comment_id, last_activity_id,
             last_openline_msg_id, deal_stage, deal_status_semantic, sync_error)
            VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)
            """,
            (deal_id, last_comment_id, last_activity_id, last_openline_msg_id,
             deal_stage, deal_status_semantic, sync_error),
        )
    else:
        await db.execute(
            """
            UPDATE deal_sync_state
            SET last_synced_at = CURRENT_TIMESTAMP,
                last_comment_id = COALESCE(?, last_comment_id),
                last_activity_id = COALESCE(?, last_activity_id),
                last_openline_msg_id = COALESCE(?, last_openline_msg_id),
                deal_stage = COALESCE(?, deal_stage),
                deal_status_semantic = COALESCE(?, deal_status_semantic),
                sync_error = ?
            WHERE deal_id = ?
            """,
            (last_comment_id, last_activity_id, last_openline_msg_id,
             deal_stage, deal_status_semantic, sync_error, deal_id),
        )
    await db.commit()


async def deals_overdue_for_sync(older_than_minutes: int = 60, limit: int = 200) -> List[int]:
    """ID-шники сделок, которые не синхронизировались дольше N минут.
    Используется в часовом cron'е: берём 200 самых «старых», пробегаем."""
    rows = await db.fetch_all(
        f"""
        SELECT deal_id
        FROM deal_sync_state
        WHERE last_synced_at < datetime('now', '-{older_than_minutes} minutes')
          AND deal_status_semantic IN ('P', NULL, '')
        ORDER BY last_synced_at ASC
        LIMIT ?
        """,
        (limit,),
    )
    return [r["deal_id"] for r in rows or []]


# ---------- статистика для /admin ------------------------------------------

async def stats() -> Dict[str, int]:
    """Сколько чего в БД — для проверки backfill'а и здоровья модуля."""
    rows = await db.fetch_all(
        """
        SELECT source_type, COUNT(*) AS n
        FROM deal_communications
        GROUP BY source_type
        """
    )
    by_type = {r["source_type"]: r["n"] for r in rows or []}

    pending_row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM deal_communications WHERE transcription_status = 'pending'"
    )
    deals_row = await db.fetch_one("SELECT COUNT(*) AS n FROM deal_sync_state")
    return {
        "total": sum(by_type.values()),
        "by_type": by_type,
        "transcription_pending": pending_row["n"] if pending_row else 0,
        "deals_tracked": deals_row["n"] if deals_row else 0,
    }
