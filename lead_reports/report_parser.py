"""Parser for the 'Онлайн отчетность' lead reports posted by the Amely bot.

The format is rigid (one `Поле: значение` per line), so plain regex is
enough — no LLM. Real sample::

    Онлайн отчетность
    Время звонка: 2026-05-20 15:36:00
    ИНН:
    Компания: Автополив
    Телефон: 79854548966
    Должность:
    ФИО: Юна
    Телефон ЛПР:
    Электронная почта:
    Город: 339
    Комментарий: 30 соток, нет плана ... (может быть в несколько строк)
    Ссылка на запись разговора        <- MessageEntityTextUrl

The MP3 link is NOT in the text — it's the URL of the
MessageEntityTextUrl entity anchored on 'Ссылка на запись разговора'.
"""

import re
from typing import Any, Dict, List, Optional

# First non-empty line of a real report.
_REPORT_MARKER = "онлайн отчетност"

# Russian field label → normalized key. Order doesn't matter, lookup is by label.
_FIELD_MAP = {
    "время звонка": "call_datetime",
    "инн": "inn",
    "компания": "company",
    "телефон": "phone",
    "должность": "position",
    "фио": "fio",
    "телефон лпр": "lpr_phone",
    "электронная почта": "email",
    "город": "city",
    "комментарий": "comment",
}

# Anchor line after which the recording link sits — also marks end of comment.
_RECORDING_ANCHOR = "ссылка на запись разговора"

_FIELD_RE = re.compile(r"^([^:]{2,40}):\s?(.*)$")


def is_report(text: Optional[str]) -> bool:
    """True if the message looks like an 'Онлайн отчетность' report."""
    if not text:
        return False
    for line in text.splitlines():
        s = line.strip()
        if s:
            return s.lower().startswith(_REPORT_MARKER)
    return False


def extract_urls(entities: Optional[List[Any]]) -> List[str]:
    """Pull .url from Telethon MessageEntityTextUrl entities.

    Kept telethon-agnostic: any object exposing a truthy `.url` attribute
    counts, so the parser stays unit-testable without telethon imports.
    """
    urls: List[str] = []
    for e in entities or []:
        url = getattr(e, "url", None)
        if url:
            urls.append(url)
    return urls


def _pick_recording_url(urls: List[str]) -> Optional[str]:
    """Prefer the ccp.center MP3 link; fall back to the first URL."""
    for u in urls:
        if "ccp.center" in u.lower() or u.lower().endswith(".mp3"):
            return u
    return urls[0] if urls else None


def parse_report(text: Optional[str], urls: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Parse one report message → dict of fields, or None if not a report.

    `urls` — list of URLs already extracted from the message entities
    (use extract_urls() on msg.entities). The comment may span several
    lines; everything between 'Комментарий:' and the recording anchor
    (or end of text) is captured.
    """
    if not is_report(text):
        return None

    lines = text.splitlines()
    result: Dict[str, Any] = {key: "" for key in _FIELD_MAP.values()}

    comment_parts: List[str] = []
    in_comment = False

    for line in lines:
        stripped = line.strip()
        low = stripped.lower()

        # Recording anchor — ends the comment, nothing useful after it.
        if low.startswith(_RECORDING_ANCHOR):
            in_comment = False
            continue

        m = _FIELD_RE.match(stripped)
        label = m.group(1).strip().lower() if m else None

        if label in _FIELD_MAP:
            key = _FIELD_MAP[label]
            value = m.group(2).strip()
            if key == "comment":
                in_comment = True
                comment_parts = [value] if value else []
            else:
                in_comment = False
                result[key] = value
        elif in_comment:
            # Continuation line of a multi-line comment.
            comment_parts.append(stripped)

    result["comment"] = "\n".join(p for p in comment_parts if p).strip()
    result["recording_url"] = _pick_recording_url(urls or [])
    return result
