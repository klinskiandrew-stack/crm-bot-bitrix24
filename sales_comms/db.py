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
               subject, text, duration_sec, transcription_status, raw_meta
        FROM deal_communications
        WHERE deal_id = ?
        ORDER BY occurred_at DESC
        LIMIT ?
        """,
        (deal_id, max_items),
    )
    out = []
    for r in rows or []:
        d = dict(r)
        # Разворачиваем raw_meta JSON в dict, чтобы digest мог читать
        # call_outcome / completed / status без повторного парсинга.
        raw = d.get("raw_meta")
        if raw:
            try:
                d["meta"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d["meta"] = {}
        else:
            d["meta"] = {}
        out.append(d)
    return out


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


# ============================================================
# Lv3: расширенный синк — контакты / файлы / счета / стадии.
# ============================================================

# ---------- contacts ----------

async def upsert_contacts(deal_id: int, contacts: List[Dict[str, Any]]) -> int:
    """Полная замена контактов сделки. Возвращает число вставленных.
    Принимаем dict с ключами: id, name, phone, email, position, company, is_primary."""
    if not contacts:
        return 0
    await db.execute("DELETE FROM deal_contacts WHERE deal_id = ?", (deal_id,))
    added = 0
    for c in contacts:
        try:
            cid = int(c.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if cid <= 0:
            continue
        await db.execute(
            """
            INSERT OR REPLACE INTO deal_contacts
            (deal_id, contact_id, name, phone, email, position, company, is_primary, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                deal_id, cid,
                c.get("name") or None,
                c.get("phone") or None,
                c.get("email") or None,
                c.get("position") or None,
                c.get("company") or None,
                1 if c.get("is_primary") else 0,
            ),
        )
        added += 1
    await db.commit()
    return added


async def contacts_for_deal(deal_id: int) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT contact_id, name, phone, email, position, company, is_primary "
        "FROM deal_contacts WHERE deal_id = ? ORDER BY is_primary DESC, contact_id",
        (deal_id,),
    )
    return [dict(r) for r in rows or []]


# ---------- files ----------

async def upsert_files(deal_id: int, files: List[Dict[str, Any]]) -> int:
    """Insert OR IGNORE by (deal_id, file_id). Возвращает число новых."""
    if not files:
        return 0
    added = 0
    for f in files:
        fid = str(f.get("file_id") or "").strip()
        name = f.get("name") or ""
        if not fid or not name:
            continue
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO deal_files
            (deal_id, file_id, name, size_bytes, mime_type, uploaded_at,
             uploaded_by, activity_id, download_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deal_id, fid, name,
                f.get("size_bytes"),
                f.get("mime_type"),
                f.get("uploaded_at"),
                f.get("uploaded_by"),
                f.get("activity_id"),
                f.get("download_url"),
            ),
        )
        if cur.rowcount and cur.rowcount > 0:
            added += 1
    await db.commit()
    return added


async def files_for_deal(deal_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT file_id, name, size_bytes, mime_type, uploaded_at, uploaded_by, activity_id "
        "FROM deal_files WHERE deal_id = ? ORDER BY uploaded_at DESC LIMIT ?",
        (deal_id, limit),
    )
    return [dict(r) for r in rows or []]


# ---------- invoices ----------

async def upsert_invoices(deal_id: int, invoices: List[Dict[str, Any]]) -> int:
    if not invoices:
        return 0
    added = 0
    for inv in invoices:
        try:
            iid = int(inv.get("invoice_id") or 0)
        except (TypeError, ValueError):
            continue
        if iid <= 0:
            continue
        cur = await db.execute(
            """
            INSERT OR REPLACE INTO deal_invoices
            (deal_id, invoice_id, invoice_number, amount_rub, currency,
             status_id, status_name, created_at_b24, paid_at, due_at, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                deal_id, iid,
                inv.get("invoice_number"),
                inv.get("amount_rub"),
                inv.get("currency") or "RUB",
                inv.get("status_id"),
                inv.get("status_name"),
                inv.get("created_at_b24"),
                inv.get("paid_at"),
                inv.get("due_at"),
            ),
        )
        if cur.rowcount and cur.rowcount > 0:
            added += 1
    await db.commit()
    return added


async def invoices_for_deal(deal_id: int) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT invoice_id, invoice_number, amount_rub, status_id, status_name, "
        "created_at_b24, paid_at, due_at "
        "FROM deal_invoices WHERE deal_id = ? ORDER BY created_at_b24 DESC",
        (deal_id,),
    )
    return [dict(r) for r in rows or []]


# ---------- stage history ----------

async def upsert_stage_history(deal_id: int, items: List[Dict[str, Any]]) -> int:
    """items: [{stage_id, stage_name, entered_at, exited_at, duration_days}, ...]
    Insert OR REPLACE по уникальному ключу (deal_id, stage_id, entered_at)."""
    if not items:
        return 0
    # Полная замена цепочки стадий — нам важно чтобы текущая
    # (exited_at=NULL) была единственной. Поэтому удаляем существующее
    # и вставляем заново.
    await db.execute("DELETE FROM deal_stage_history WHERE deal_id = ?", (deal_id,))
    added = 0
    for it in items:
        sid = it.get("stage_id")
        ent = it.get("entered_at")
        if not sid or not ent:
            continue
        await db.execute(
            """
            INSERT OR REPLACE INTO deal_stage_history
            (deal_id, stage_id, stage_name, entered_at, exited_at, duration_days, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                deal_id, sid,
                it.get("stage_name"),
                ent,
                it.get("exited_at"),
                it.get("duration_days"),
            ),
        )
        added += 1
    await db.commit()
    return added


async def stage_history_for_deal(deal_id: int) -> List[Dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT stage_id, stage_name, entered_at, exited_at, duration_days "
        "FROM deal_stage_history WHERE deal_id = ? ORDER BY entered_at ASC",
        (deal_id,),
    )
    return [dict(r) for r in rows or []]


# ---------- composite full context (для digest) -----------------------------

async def full_deal_context(deal_id: int) -> Dict[str, Any]:
    """Одним вызовом — всё что нужно digest'у по сделке.
    Возвращает {comms, contacts, files, invoices, stages, sync_state}."""
    return {
        "comms": await communications_for_deal(deal_id, max_items=20),
        "contacts": await contacts_for_deal(deal_id),
        "files": await files_for_deal(deal_id, limit=12),
        "invoices": await invoices_for_deal(deal_id),
        "stages": await stage_history_for_deal(deal_id),
        "sync_state": await get_sync_state(deal_id),
    }
