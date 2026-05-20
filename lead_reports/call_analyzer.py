"""AI analysis of a call transcript via DeepSeek (JSON mode).

Takes the Whisper transcript of a sales call and returns a structured
verdict: summary, client need, manager score (1-5), lead temperature.
Context: Growzone sells landscaping services — irrigation (автополив),
rolled lawn (рулонный газон), lighting (освещение).
"""

import json
from typing import Any, Dict, Optional

import aiohttp
import structlog

from config import settings

logger = structlog.get_logger()

_TIMEOUT = aiohttp.ClientTimeout(total=120)

_SYSTEM_PROMPT = """Ты — аналитик отдела продаж компании Growzone. Компания
продаёт услуги ландшафтного благоустройства: автополив, рулонный газон,
ландшафтное освещение, фасадную подсветку.

Тебе дают расшифровку телефонного разговора менеджера с клиентом
(распознавание речи — возможны мелкие ошибки, учитывай это).

Проанализируй разговор и верни СТРОГО JSON-объект, без markdown, без
пояснений вокруг, ровно с такими полями:

{
  "summary": "2-3 предложения: о чём был звонок, чем закончился",
  "client_need": "что конкретно нужно клиенту (услуга, объём, объект)",
  "manager_score": <целое число 1-5: качество работы менеджера>,
  "manager_comment": "1-2 предложения почему такая оценка",
  "lead_temp": "горячий" | "тёплый" | "холодный"
}

Критерии manager_score: 5 — менеджер выявил потребность, отработал
возражения, договорился о следующем шаге; 1 — нагрубил, не выявил
потребность, упустил клиента. lead_temp: «горячий» — клиент готов
покупать сейчас; «тёплый» — интерес есть, нужно дожать; «холодный» —
не целевой / не интересно / далёкая перспектива."""


def is_enabled() -> bool:
    """Analysis needs a DeepSeek key."""
    return bool(settings.deepseek_api_key)


def _coerce(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise the model's JSON into the exact shape we store."""
    try:
        score = int(raw.get("manager_score"))
    except (TypeError, ValueError):
        score = None
    if score is not None:
        score = max(1, min(5, score))

    temp = str(raw.get("lead_temp") or "").strip().lower()
    if temp not in ("горячий", "тёплый", "теплый", "холодный"):
        temp = ""
    temp = temp.replace("теплый", "тёплый")

    return {
        "summary": str(raw.get("summary") or "").strip(),
        "client_need": str(raw.get("client_need") or "").strip(),
        "manager_score": score,
        "manager_comment": str(raw.get("manager_comment") or "").strip(),
        "lead_temp": temp,
    }


async def analyze(transcript: str) -> Optional[Dict[str, Any]]:
    """Analyse one transcript → dict, or None on failure.

    Returns keys: summary, client_need, manager_score, manager_comment,
    lead_temp.
    """
    if not transcript or not transcript.strip():
        return None
    if not is_enabled():
        logger.warning("Call analysis skipped — no DeepSeek key")
        return None

    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": transcript[:20000]},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.3,
    }
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error("Call analysis API error", status=resp.status, body=text[:300])
                    return None
                data = json.loads(text)
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception as e:
        logger.error("Call analysis failed", error=str(e))
        return None

    result = _coerce(parsed)
    logger.info(
        "Call analyzed",
        score=result["manager_score"],
        temp=result["lead_temp"],
        summary_chars=len(result["summary"]),
    )
    return result
