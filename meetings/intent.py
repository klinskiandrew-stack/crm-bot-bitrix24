"""Intent parser for meeting requests.

Classifies a bot @-mention into:
  - 'vote'    — день дан, точное время НЕ указано → нужно голосование
  - 'direct'  — день и точное время указаны → создать встречу сразу
  - 'none'    — это не запрос про созвон

Uses DeepSeek (JSON mode) when available, falls back to a light regex
parser when DEEPSEEK_API_KEY is empty. Always returns a `MeetingIntent`
or `None`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import structlog

from config import settings

logger = structlog.get_logger()

_MSK = ZoneInfo("Europe/Moscow")

_MEETING_KEYWORDS_RE = re.compile(
    r"созвон|встреч|зум|zoom|митинг|созвонимся|звонок|перезвон|"
    r"calls?|meeting|meet",
    re.IGNORECASE,
)


@dataclass
class MeetingIntent:
    kind: str               # 'vote' | 'direct'
    meeting_date: str       # YYYY-MM-DD (MSK)
    meeting_time: Optional[str]   # HH:MM (MSK), only for direct
    duration_min: int       # default 60
    topic: str              # short topic, may be empty


_SYSTEM_PROMPT = """Ты — парсер запросов на созвон в Telegram-боте.
Часовой пояс ВСЕГДА Москва (MSK).
Сегодня: {today} ({today_weekday}).
Рабочие часы: 10:00–18:00, шаг 1 час. Только будние дни.

На вход — фраза из чата. Определи: пользователь хочет назначить созвон?
Если да — извлеки дату, время (если указано), длительность, тему.

Верни СТРОГО JSON без пояснений:
{{
  "is_meeting_request": true/false,
  "kind": "vote" | "direct" | null,
  "date": "YYYY-MM-DD" | null,
  "time": "HH:MM" | null,
  "duration_min": 60,
  "topic": "короткая тема" | ""
}}

Правила:
- kind="direct" если в запросе ЯВНО указано конкретное время ("в 15:00", "в 3 часа дня", "в 17.00")
- kind="vote" если время НЕ указано или дано окно ("давайте созвонимся", "созвон в четверг", "созвонимся завтра")
- Длительность по умолчанию 60 минут. Если в запросе "на 30 мин", "на час", "на 2 часа" — извлеки.
- Если день не указан, но контекст про "созвонимся" — используй today, kind="vote"
- "завтра", "послезавтра", "в понедельник", "в пятницу" — конвертируй в YYYY-MM-DD относительно today
- "через неделю" — today + 7 дней
- Если запрос НЕ про созвон (вопрос про CRM, отчёт и т.п.) — is_meeting_request=false
- Тема: бери основной смысл, например "обсудить бюджет"; пустая строка если нет

Примеры:
"@grow_bot создай встречу на завтра в 17:00" → direct, дата=завтра, время=17:00
"@grow_bot собери созвон на пятницу" → vote, дата=пятница, время=null
"@grow_bot когда созвонимся в четверг на час" → vote, дата=четверг, duration=60
"@grow_bot поставь зум на 14:30 послезавтра обсудить отчёт" → direct, время=14:30, topic="обсудить отчёт"
"@grow_bot сколько сделок за май" → is_meeting_request=false
"""


def _today_msk() -> date:
    return datetime.now(_MSK).date()


def _weekday_ru(d: date) -> str:
    names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return names[d.weekday()]


async def _llm_parse(text: str) -> Optional[dict]:
    """Call DeepSeek with JSON-mode. Returns parsed dict or None on failure."""
    if not settings.deepseek_api_key:
        return None

    today = _today_msk()
    system = _SYSTEM_PROMPT.format(today=today.isoformat(), today_weekday=_weekday_ru(today))

    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "max_tokens": 300,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.warning("DeepSeek intent parse non-200", status=resp.status, body=err[:200])
                    return None
                data = await resp.json(content_type=None)
        content = data["choices"][0]["message"]["content"] or "{}"
        return json.loads(content)
    except Exception as e:
        logger.warning("DeepSeek intent parse failed, falling back", error=str(e))
        return None


# -------- regex fallback ----------

_DAY_WORDS = {
    "сегодня": 0, "завтра": 1, "послезавтра": 2,
}
_WEEKDAY_WORDS = {
    "понедельник": 0, "пн": 0,
    "вторник": 1, "вт": 1,
    "среду": 2, "среда": 2, "ср": 2,
    "четверг": 3, "чт": 3,
    "пятницу": 4, "пятница": 4, "пт": 4,
    "субботу": 5, "суббота": 5, "сб": 5,
    "воскресенье": 6, "вс": 6,
}
_TIME_RE = re.compile(
    r"\b(?:в\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(?:час|ч)?\b",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"на\s+(\d+)\s*(час|часа|часов|мин|минут)|на\s+(полчаса|час)",
    re.IGNORECASE,
)


def _next_weekday(target_wd: int, today: date) -> date:
    delta = (target_wd - today.weekday()) % 7
    if delta == 0:
        delta = 7  # "в пятницу" сказанное в пятницу обычно = следующая
    return today + timedelta(days=delta)


def _fallback_parse(text: str) -> Optional[dict]:
    """Best-effort regex parser as backup when LLM is unavailable."""
    low = text.lower()
    if not _MEETING_KEYWORDS_RE.search(low):
        return None

    today = _today_msk()
    target = None

    # Check longest words first so "послезавтра" wins over "завтра".
    for word, off in sorted(_DAY_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{word}\b", low):
            target = today + timedelta(days=off)
            break

    if target is None:
        for word, wd in _WEEKDAY_WORDS.items():
            if re.search(rf"\b{word}\b", low):
                target = _next_weekday(wd, today)
                break

    if target is None:
        target = today

    # Time
    time_str = None
    # Match patterns like "в 15:00", "в 3 часа", "в 17.30"
    for m in _TIME_RE.finditer(low):
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        if 0 <= hh <= 23 and 0 <= mm < 60:
            # Heuristic: ignore numbers that look like dates/days
            time_str = f"{hh:02d}:{mm:02d}"
            break

    # Duration
    duration = 60
    dm = _DURATION_RE.search(low)
    if dm:
        if dm.group(3) == "полчаса":
            duration = 30
        elif dm.group(3) == "час":
            duration = 60
        else:
            num = int(dm.group(1))
            unit = dm.group(2)
            if unit and unit.startswith("мин"):
                duration = num
            else:
                duration = num * 60

    return {
        "is_meeting_request": True,
        "kind": "direct" if time_str else "vote",
        "date": target.isoformat(),
        "time": time_str,
        "duration_min": duration,
        "topic": "",
    }


async def parse(text: str) -> Optional[MeetingIntent]:
    """Parse a user message. Returns MeetingIntent or None."""
    if not text or not text.strip():
        return None

    data = await _llm_parse(text)
    if not data:
        data = _fallback_parse(text)

    if not data or not data.get("is_meeting_request"):
        return None

    # Sanity checks
    kind = data.get("kind") or ("direct" if data.get("time") else "vote")
    if kind not in ("vote", "direct"):
        return None

    meeting_date = data.get("date")
    if not meeting_date or not re.match(r"^\d{4}-\d{2}-\d{2}$", meeting_date):
        return None

    meeting_time = data.get("time")
    if meeting_time and not re.match(r"^\d{2}:\d{2}$", meeting_time):
        meeting_time = None

    duration = int(data.get("duration_min") or 60)
    if duration < 15:
        duration = 60
    if duration > 240:
        duration = 240

    topic = (data.get("topic") or "").strip()[:120]

    return MeetingIntent(
        kind=kind,
        meeting_date=meeting_date,
        meeting_time=meeting_time,
        duration_min=duration,
        topic=topic,
    )
