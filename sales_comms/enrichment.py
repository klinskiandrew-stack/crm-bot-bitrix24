"""Lv3 расширенный синк: контакты / файлы / счета / история стадий.

Отделено от collector.py чтобы основной поток (комменты + активности +
OL) оставался простым и быстрым. enrichment вызывается из sync_deal
после успешного основного синка — если упадёт, основной синк уже
сохранён.

Все вызовы non-blocking: при ошибке Bitrix логируем warning и идём
дальше. Цель — лучшее обогащение, но не критическое.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog

from b24.client import Bitrix24Client
from sales_comms.db import (
    upsert_contacts,
    upsert_files,
    upsert_invoices,
    upsert_stage_history,
)

logger = structlog.get_logger()


# ----- helpers --------------------------------------------------------------

def _phones_to_str(phones: Any) -> Optional[str]:
    """Bitrix даёт PHONE как [{VALUE: "+7...", VALUE_TYPE: "WORK"}, ...].
    Склеиваем в строку «; »-разделённую."""
    if not phones or not isinstance(phones, list):
        return None
    out = []
    for p in phones:
        if isinstance(p, dict) and p.get("VALUE"):
            out.append(str(p["VALUE"]))
    return "; ".join(out) if out else None


def _emails_to_str(emails: Any) -> Optional[str]:
    return _phones_to_str(emails)   # тот же формат


def _parse_dt(s: Any) -> Optional[str]:
    """Bitrix ISO datetime → unified ISO string (для SQLite). Без tz."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt.isoformat()
    except (ValueError, TypeError):
        return str(s)


# ----- contacts ------------------------------------------------------------

async def _fetch_deal_contacts(client: Bitrix24Client, deal_id: int) -> List[Dict[str, Any]]:
    """Контакты сделки. Сначала список связей crm.deal.contact.items.get,
    потом по каждому contact_id — детали через crm.contact.get."""
    # связи
    link_resp = await client._call("crm.deal.contact.items.get", {"id": deal_id})
    if not isinstance(link_resp, dict) or "error" in link_resp:
        return []
    links = link_resp.get("result") or []
    if not isinstance(links, list):
        return []

    contacts: List[Dict[str, Any]] = []
    for link in links:
        try:
            cid = int(link.get("CONTACT_ID") or 0)
        except (TypeError, ValueError):
            continue
        if cid <= 0:
            continue
        full = await client.get_contact(cid)
        if not isinstance(full, dict) or "error" in full:
            continue
        name_parts = [
            full.get("LAST_NAME"), full.get("NAME"), full.get("SECOND_NAME"),
        ]
        name = " ".join(p for p in name_parts if p).strip() or None
        contacts.append({
            "id": cid,
            "name": name,
            "phone": _phones_to_str(full.get("PHONE")),
            "email": _emails_to_str(full.get("EMAIL")),
            "position": full.get("POST"),
            "company": None,   # COMPANY_ID отдельный запрос — пока пропускаем
            "is_primary": link.get("IS_PRIMARY") == "Y",
        })
    return contacts


# ----- files (из уже полученных activities) --------------------------------

def _extract_files_from_activities(
    deal_id: int, activities: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """FILES уже приходят в crm.activity.list (мы запрашиваем поле FILES).
    Дополнительный запрос не нужен — просто перепаковываем."""
    files: List[Dict[str, Any]] = []
    for a in activities or []:
        att = a.get("FILES")
        if not isinstance(att, list):
            continue
        aid = str(a.get("ID") or "")
        for f in att:
            if not isinstance(f, dict):
                continue
            fid = f.get("id") or f.get("ID")
            name = f.get("name") or f.get("NAME") or "файл"
            if not fid:
                continue
            files.append({
                "file_id": str(fid),
                "name": str(name),
                "size_bytes": f.get("size"),
                "mime_type": None,   # disk.file.get вернул бы, но запрос дорогой
                "uploaded_at": _parse_dt(a.get("CREATED") or a.get("START_TIME")),
                "uploaded_by": a.get("RESPONSIBLE_ID"),
                "activity_id": aid,
                "download_url": None,  # короткоживущий, генерим лениво
            })
    return files


# ----- invoices ------------------------------------------------------------

async def _fetch_deal_invoices(client: Bitrix24Client, deal_id: int) -> List[Dict[str, Any]]:
    """Счета — пробуем smart-invoice (entityTypeId=31), потом legacy."""
    raw = await client.get_deal_invoices(deal_id)
    out: List[Dict[str, Any]] = []
    for inv in raw or []:
        if not isinstance(inv, dict):
            continue
        # smart-invoice: id, title, opportunity, currencyId, stageId, createdTime, closedate
        # legacy:        ID, ACCOUNT_NUMBER, PRICE, CURRENCY, STATUS_ID, DATE_INSERT, DATE_PAYED
        if "id" in inv:        # smart
            iid = inv.get("id")
            number = inv.get("title")
            amount = inv.get("opportunity")
            currency = inv.get("currencyId") or "RUB"
            status_id = inv.get("stageId")
            created = inv.get("createdTime")
            paid = inv.get("closedate")   # close date ≠ paid date, но ближайшее
            due = None
        else:                    # legacy
            iid = inv.get("ID")
            number = inv.get("ACCOUNT_NUMBER")
            amount = inv.get("PRICE")
            currency = inv.get("CURRENCY") or "RUB"
            status_id = inv.get("STATUS_ID")
            created = inv.get("DATE_INSERT")
            paid = inv.get("DATE_PAYED") or inv.get("PAY_VOUCHER_DATE")
            due = None
        try:
            iid_int = int(iid)
        except (TypeError, ValueError):
            continue
        try:
            amount_f = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount_f = None
        # человекочитаемый статус
        status_name = {
            "P": "оплачен", "PAID": "оплачен",
            "N": "новый", "DRAFT": "черновик",
            "D": "просрочен",
            "S": "выставлен", "SENT": "выставлен",
            "C": "отменён", "CANCELED": "отменён",
        }.get(str(status_id or "").upper(), str(status_id or ""))
        out.append({
            "invoice_id": iid_int,
            "invoice_number": str(number) if number else None,
            "amount_rub": amount_f,
            "currency": currency,
            "status_id": str(status_id) if status_id else None,
            "status_name": status_name,
            "created_at_b24": _parse_dt(created),
            "paid_at": _parse_dt(paid),
            "due_at": _parse_dt(due),
        })
    return out


# ----- stage history -------------------------------------------------------

# Маппинг STAGE_ID → русское имя. Загружается лениво из b24 один раз
# через crm.dealcategory.stage.list, кэшируется в _stage_names.
_stage_names: Optional[Dict[str, str]] = None


async def _load_stage_names(client: Bitrix24Client) -> Dict[str, str]:
    global _stage_names
    if _stage_names is not None:
        return _stage_names
    out: Dict[str, str] = {}
    # main pipeline (category=0)
    resp = await client._call("crm.dealcategory.stage.list", {"id": 0})
    if isinstance(resp, dict) and "result" in resp:
        for s in resp["result"] or []:
            if isinstance(s, dict) and s.get("STATUS_ID"):
                out[str(s["STATUS_ID"])] = s.get("NAME") or s["STATUS_ID"]
    _stage_names = out
    return out


async def _fetch_stage_history(client: Bitrix24Client, deal_id: int) -> List[Dict[str, Any]]:
    """Хронология стадий сделки: для каждой stage запишем когда вошла и
    сколько провисела. Получаем сырые события и вычисляем длительности."""
    events = await client.get_deal_stage_history(deal_id)
    if not events:
        return []
    stage_names = await _load_stage_names(client)
    items: List[Dict[str, Any]] = []
    # events отсортированы ASC по CREATED_TIME
    parsed = [
        (e.get("STAGE_ID"), _parse_dt(e.get("CREATED_TIME")))
        for e in events if e.get("STAGE_ID") and e.get("CREATED_TIME")
    ]
    if not parsed:
        return []
    now_iso = datetime.now().replace(tzinfo=None).isoformat()
    for i, (sid, entered) in enumerate(parsed):
        exited = parsed[i + 1][1] if i + 1 < len(parsed) else None
        # длительность в днях
        try:
            ent_dt = datetime.fromisoformat(entered)
            end_dt = datetime.fromisoformat(exited) if exited else datetime.now().replace(tzinfo=None)
            duration_days = max(0, (end_dt - ent_dt).days)
        except Exception:
            duration_days = None
        items.append({
            "stage_id": sid,
            "stage_name": stage_names.get(sid, sid),
            "entered_at": entered,
            "exited_at": exited,
            "duration_days": duration_days,
        })
    return items


# ----- главная точка входа -------------------------------------------------

async def enrich_deal(
    client: Bitrix24Client,
    deal_id: int,
    activities: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, int]:
    """Дополнить сделку контактами, файлами, счетами и историей стадий.

    activities — те же что collector получил из crm.activity.list (мы их
    переиспользуем для files, чтобы не делать второй запрос).

    Возвращает {contacts, files, invoices, stages} — счётчики добавленных
    записей. Не падает — каждая часть в своём try/except.
    """
    res = {"contacts": 0, "files": 0, "invoices": 0, "stages": 0}

    # contacts
    try:
        contacts = await _fetch_deal_contacts(client, deal_id)
        if contacts:
            res["contacts"] = await upsert_contacts(deal_id, contacts)
    except Exception as e:
        logger.warning("enrich: contacts failed", deal_id=deal_id, error=str(e))

    # files (из уже-полученных activities — без дополнительного запроса)
    try:
        files = _extract_files_from_activities(deal_id, activities or [])
        if files:
            res["files"] = await upsert_files(deal_id, files)
    except Exception as e:
        logger.warning("enrich: files failed", deal_id=deal_id, error=str(e))

    # invoices
    try:
        invs = await _fetch_deal_invoices(client, deal_id)
        if invs:
            res["invoices"] = await upsert_invoices(deal_id, invs)
    except Exception as e:
        logger.warning("enrich: invoices failed", deal_id=deal_id, error=str(e))

    # stages
    try:
        hist = await _fetch_stage_history(client, deal_id)
        if hist:
            res["stages"] = await upsert_stage_history(deal_id, hist)
    except Exception as e:
        logger.warning("enrich: stages failed", deal_id=deal_id, error=str(e))

    return res
