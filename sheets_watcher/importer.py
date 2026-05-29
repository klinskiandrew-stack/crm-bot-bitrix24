"""Импорт лидов из Google Sheets в Bitrix24.

Разбирает строку таблицы:
  Колонка "Имя:" обычно содержит «Имя, остальное описание» —
  первое слово до запятой → NAME, остаток → COMMENTS карточки лида.

Создаёт лид через crm.lead.add:
  TITLE       = «{name} — Перехват (temshik)»
  NAME        = имя
  COMMENTS    = остальная часть описания
  PHONE       = из колонки «Телефон:»
  SOURCE_ID   = 8  (Перехват конкурентов)
  UTM_SOURCE  = temshik

Дедупликация — таблица sheet_lead_imports с UNIQUE(sheet_id, external_id),
где external_id берётся из колонки «Заявка:». Повторный прогон одних и
тех же строк никогда не создаёт дублей.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import structlog

from b24.client import Bitrix24Client
from db.connection import db

logger = structlog.get_logger()


# Маппинг колонок (по имени заголовка) → ключ в parsed row.
# Если у таблицы немного другой порядок — добавим алиасы.
_HEADER_ALIASES = {
    "№": "row_no",
    "Заявка:": "external_id",
    "Заявка": "external_id",
    "Имя:": "name_raw",
    "Имя": "name_raw",
    "Телефон:": "phone",
    "Телефон": "phone",
    "Время:": "time",
    "Время": "time",
    "Проект:": "project",
    "Проект": "project",
}


def _normalize_phone(raw: str) -> str:
    """Простая нормализация: только цифры, '+' впереди если 11 символов."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return ""
    # 79... → +79..., 89... → +79...
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    return digits


def _split_name_and_comment(name_raw: str) -> Tuple[str, str]:
    """«Олег, Москва, схема есть» → ("Олег", "Москва, схема есть")
    «Артём» → ("Артём", "")"""
    if not name_raw:
        return "", ""
    s = name_raw.strip()
    if "," in s:
        head, tail = s.split(",", 1)
        return head.strip(), tail.strip()
    return s, ""


def _parse_row(row: List[str], headers: List[str]) -> Dict[str, str]:
    """row → {row_no, external_id, name_raw, phone, time, project}."""
    out: Dict[str, str] = {}
    for i, h in enumerate(headers):
        key = _HEADER_ALIASES.get(h.strip())
        if not key or i >= len(row):
            continue
        out[key] = (row[i] or "").strip()
    return out


async def _is_already_imported(sheet_id: str, external_id: str) -> bool:
    if not external_id:
        return False
    row = await db.fetch_one(
        "SELECT 1 FROM sheet_lead_imports WHERE sheet_id = ? AND external_id = ?",
        (sheet_id, external_id),
    )
    return bool(row)


async def _record_import(
    sheet_id: str, external_id: str, row_idx: int,
    b24_lead_id: Optional[int], title: str, phone: str, error: Optional[str],
) -> None:
    await db.execute(
        """
        INSERT OR REPLACE INTO sheet_lead_imports
        (sheet_id, external_id, row_index, b24_lead_id, title, phone, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (sheet_id, external_id, row_idx, b24_lead_id, title, phone, error),
    )
    await db.commit()


async def _create_lead(
    client: Bitrix24Client,
    parsed: Dict[str, str],
    source_id: str,
    utm_source: str,
) -> Optional[int]:
    """Создать лид через crm.lead.add. Возвращает id или None."""
    name, comment_extra = _split_name_and_comment(parsed.get("name_raw", ""))
    phone = _normalize_phone(parsed.get("phone", ""))
    time_str = parsed.get("time", "")
    project = parsed.get("project", "")
    external_id = parsed.get("external_id", "")

    # COMMENTS: соединяем «остальная часть имени» + время + проект + ID заявки.
    comment_parts = []
    if comment_extra:
        comment_parts.append(comment_extra)
    if time_str:
        comment_parts.append(f"Время заявки: {time_str}")
    if project:
        comment_parts.append(f"Проект: {project}")
    if external_id:
        comment_parts.append(f"ID заявки: {external_id}")
    comments = "\n".join(comment_parts)

    title = f"{name or 'Без имени'} — Перехват"
    if utm_source:
        title += f" ({utm_source})"

    fields: Dict[str, Any] = {
        "TITLE": title,
        "NAME": name or "Без имени",
        "SOURCE_ID": source_id,
        "COMMENTS": comments,
    }
    if phone:
        fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
    if utm_source:
        fields["UTM_SOURCE"] = utm_source

    resp = await client._call("crm.lead.add", {"fields": fields})
    if not isinstance(resp, dict) or "error" in resp:
        err = resp.get("error") if isinstance(resp, dict) else "no response"
        logger.error("crm.lead.add failed", error=err, fields=fields)
        return None
    lead_id = resp.get("result")
    try:
        return int(lead_id) if lead_id else None
    except (TypeError, ValueError):
        return None


async def import_new_rows(
    client: Bitrix24Client,
    rows: List[List[str]],
    *,
    sheet_id: str,
    source_id: str = "8",
    utm_source: str = "temshik",
) -> Dict[str, Any]:
    """Главный entry. Принимает rows ИЗ Google Sheets целиком (включая
    заголовок), создаёт в Bitrix лиды для тех строк, которых ещё нет в
    sheet_lead_imports. Возвращает счётчики + список созданных лидов
    для уведомления админа."""
    if not rows or len(rows) < 2:
        return {"created": [], "skipped": 0, "failed": 0}

    headers = rows[0]
    created: List[Dict[str, Any]] = []
    skipped = 0
    failed = 0

    for idx, row in enumerate(rows[1:], start=2):  # row_index в таблице 1-based, +1 за заголовок
        parsed = _parse_row(row, headers)
        external_id = parsed.get("external_id", "")
        if not external_id:
            # Нет ID заявки — рискуем создать дубли, пропускаем
            logger.info("sheet row has no external_id — skipping",
                        sheet_id=sheet_id, row_index=idx)
            skipped += 1
            continue
        if await _is_already_imported(sheet_id, external_id):
            skipped += 1
            continue

        try:
            lead_id = await _create_lead(client, parsed, source_id, utm_source)
        except Exception as e:
            logger.error("create_lead exception", error=str(e), row_index=idx)
            lead_id = None
            await _record_import(
                sheet_id, external_id, idx, None,
                parsed.get("name_raw", "")[:120], parsed.get("phone", ""),
                error=str(e)[:200],
            )
            failed += 1
            continue

        title = parsed.get("name_raw", "")[:120]
        phone = _normalize_phone(parsed.get("phone", ""))
        await _record_import(
            sheet_id, external_id, idx, lead_id, title, phone,
            error=None if lead_id else "crm.lead.add returned None",
        )
        if lead_id:
            created.append({
                "lead_id": lead_id,
                "external_id": external_id,
                "title": title,
                "phone": phone,
                "row_index": idx,
            })
        else:
            failed += 1

    return {"created": created, "skipped": skipped, "failed": failed}
