"""AI analysis of a call transcript via DeepSeek (JSON mode).

Takes the Whisper transcript of a call-centre call and returns a
structured verdict: summary, client need, manager score (1-5), lead
temperature. The score is NOT based on the model's own opinion — the
operator is graded against the official call-centre script
(call_script.md), so the evaluation is consistent and objective.

The script is loaded from call_script.md on every analysis, so updating
the script (a new version from the call centre) takes effect without a
code change or restart — just replace the file.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
import structlog

from config import settings

logger = structlog.get_logger()

_TIMEOUT = aiohttp.ClientTimeout(total=120)

# Default script — the call-centre перехват regulations.
# Other call types (manager-vs-client calls from Mango) pass a different
# script path to `analyze`.
_DEFAULT_SCRIPT_PATH = Path(__file__).parent / "call_script.md"


def _load_script(path: Optional[Path] = None) -> str:
    """Read a script file. Empty string if missing — analysis falls back
    to generic criteria rather than breaking."""
    p = Path(path) if path else _DEFAULT_SCRIPT_PATH
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("Call script not loaded", path=str(p), error=str(e))
        return ""


_JSON_SHAPE = """Верни СТРОГО JSON-объект, без markdown и без пояснений вокруг,
ровно с такими полями:

{
  "summary": "2-3 предложения: о чём был звонок и чем закончился",
  "client_need": "что конкретно нужно клиенту (услуга, объём, объект)",
  "manager_score": <целое число 1-5>,
  "manager_comment": "чёткое обоснование оценки по скрипту",
  "lead_temp": "горячий" | "тёплый" | "холодный"
}

lead_temp — по готовности клиента: «горячий» — готов покупать сейчас;
«тёплый» — интерес есть, нужно дожать; «холодный» — не целевой / не
интересно / далёкая перспектива."""


def _build_system_prompt(script: str, role: str = "оператор") -> str:
    """System prompt for the analysis. With a script — grade strictly by
    script adherence; without — fall back to generic criteria.

    `role` подставляется в текст вопроса о том, кого мы оцениваем:
    «оператор» для звонков КЦ, «менеджер» для звонков менеджера с клиентом.
    """
    if script:
        return f"""Ты — специалист отдела контроля качества компании Growzone
(благоустройство: автополив, газон, освещение, фасадная подсветка).

В компании есть утверждённые регламенты работы со звонками. Тебе дают
расшифровку одного звонка (распознавание речи — возможны мелкие ошибки,
учитывай это).

Твоя задача — оценить, насколько {role} СЛЕДОВАЛ РЕГЛАМЕНТУ. НЕ придумывай
собственные критерии и НЕ оценивай «по ощущениям» — сверяй разговор
строго с регламентом ниже.

================== РЕГЛАМЕНТ (ЭТАЛОН) ==================
{script}
================== КОНЕЦ РЕГЛАМЕНТА ==================

{_JSON_SHAPE}

manager_score — СТРОГО по соответствию регламенту:
- 5 — {role} отработал по регламенту полностью: все ключевые шаги
  выполнены, без отклонений.
- 4 — каркас регламента соблюдён, 1-2 незначительных пропуска.
- 3 — соблюдена основа, но заметные пропуски: пропущены отдельные
  обязательные шаги, ослаблена отработка ключевых моментов.
- 2 — грубые отклонения: пропущена значимая часть регламента (квалификация,
  отработка возражений, договорённость о следующем шаге).
- 1 — {role} фактически не работал по регламенту: не представился, нагрубил,
  не выявил потребность, упустил клиента.

В manager_comment перечисли КОНКРЕТНО, со ссылкой на пункты регламента: что
{role} сделал, а что пропустил или нарушил. Оценивай только {role}а,
не клиента."""

    # Fallback — script file missing.
    return f"""Ты — аналитик отдела контроля качества компании Growzone
(благоустройство: автополив, газон, освещение, фасадная подсветка).
Тебе дают расшифровку звонка оператора с клиентом (распознавание речи —
возможны ошибки).

{_JSON_SHAPE}

manager_score: 5 — оператор выявил потребность, отработал возражения,
договорился о следующем шаге; 1 — нагрубил, не выявил потребность,
упустил клиента."""


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


async def analyze(
    transcript: str,
    script_path: Optional[Path] = None,
    role: str = "оператор",
) -> Optional[Dict[str, Any]]:
    """Analyse one transcript → dict, or None on failure.

    Returns keys: summary, client_need, manager_score, manager_comment,
    lead_temp. manager_score reflects adherence to the given script.

    `script_path` — путь к файлу-эталону (markdown). По умолчанию — КЦ-скрипт
    `call_script.md` рядом с этим модулем. Для звонков менеджеров передавай
    `manager_call_script.md`.
    `role` — кого мы оцениваем: «оператор» (КЦ) или «менеджер» (звонки по сделке).
    """
    if not transcript or not transcript.strip():
        return None
    if not is_enabled():
        logger.warning("Call analysis skipped — no DeepSeek key")
        return None

    system_prompt = _build_system_prompt(_load_script(script_path), role=role)

    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Расшифровка звонка:\n\n" + transcript[:20000]},
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
