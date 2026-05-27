"""Daily self-monitoring — two modes.

build_error_digest / send_error_digest  — factual summary (groups by
category, no LLM). Used by /debug command for instant checks.

build_error_diagnosis / send_error_diagnosis — AI-powered review:
audit_log + journalctl → DeepSeek → root causes + suggested fixes,
delivered as HTML to the admin's DM. Runs daily at 08:30 MSK.
"""

import asyncio
import json
import re
from typing import Dict, List, Tuple

import aiohttp
import structlog
from aiogram import Bot
from aiogram.enums import ParseMode

from config import settings
from db.connection import db

logger = structlog.get_logger()


def _categorise(error: str) -> str:
    """Map a raw error string to a human-readable category."""
    e = (error or "").lower()
    if "circuit_breaker" in e:
        return "Сработал предохранитель (лимит токенов/шагов/вызовов)"
    if "max iterations" in e:
        return "Исчерпан лимит итераций"
    if "message text is empty" in e or "message is empty" in e:
        return "Пустой ответ боту"
    if "deepseek" in e or "kie" in e:
        return "Ошибка LLM API"
    if "bitrix" in e:
        return "Ошибка Bitrix24"
    if "timeout" in e or "timed out" in e:
        return "Таймаут"
    return f"Прочее: {(error or '')[:60]}"


async def build_error_digest(hours: int = 24) -> str:
    """Build the digest text for the last `hours` of audit_log."""
    rows = await db.fetch_all(
        "SELECT question, error FROM audit_log "
        "WHERE created_at >= datetime('now', ?)",
        (f"-{hours} hours",),
    )
    total = len(rows)
    failed = [r for r in rows if r["error"]]

    if not failed:
        return (
            f"🔍 <b>Отладка бота</b> — за {hours} ч\n\n"
            f"Запросов: <b>{total}</b>\n"
            f"✅ Ошибок нет."
        )

    # Group by category → list of example questions.
    groups: Dict[str, List[str]] = {}
    for r in failed:
        cat = _categorise(r["error"])
        groups.setdefault(cat, [])
        q = (r["question"] or "").strip().replace("\n", " ")
        if q:
            groups[cat].append(q)

    ordered: List[Tuple[str, List[str]]] = sorted(
        groups.items(), key=lambda kv: len(kv[1]), reverse=True
    )

    pct = round(len(failed) / total * 100, 1) if total else 0
    lines = [
        f"🔍 <b>Отладка бота</b> — за {hours} ч\n",
        f"Запросов: <b>{total}</b>",
        f"Ошибок: <b>{len(failed)}</b> ({pct}%)\n",
        "<b>По типам:</b>",
    ]
    for cat, questions in ordered:
        lines.append(f"\n• {cat} — <b>{len(questions)}</b>")
        for q in questions[:2]:  # up to 2 example questions
            short = q[:80] + ("…" if len(q) > 80 else "")
            lines.append(f"   — «{short}»")

    lines.append(
        "\n\nДля разбора причин и решений — напиши «сделай отладку бота»."
    )
    return "\n".join(lines)


async def send_error_digest(bot: Bot, hours: int = 24) -> None:
    """Send the digest to the admin."""
    if not settings.admin_telegram_id:
        logger.warning("No admin_telegram_id — skipping error digest")
        return
    try:
        text = await build_error_digest(hours=hours)
        await bot.send_message(
            settings.admin_telegram_id, text, parse_mode=ParseMode.HTML
        )
        logger.info("Error digest sent", hours=hours)
    except Exception as e:
        logger.error("Failed to send error digest", error=str(e))


# ───────────────────────────── AI diagnosis ──────────────────────────────


_JOURNAL_UNIT = "crm-bot"
_JOURNAL_MAX_BYTES = 60_000   # отдаём LLM не больше ~15k токенов с лога
_AUDIT_MAX_ROWS = 25          # топ-N запросов с ошибками — этого хватает для разбора
_TG_MSG_LIMIT = 3800          # запас под HTML-теги, telegram лимит 4096


async def _read_journalctl(hours: int) -> str:
    """Pull recent crm-bot journal entries. Empty string if unavailable.

    На сервере systemd-журнал доступен пользователю crmbot только если он в
    группе systemd-journal или adm. Если нет — функция вернёт пометку и не
    упадёт. См. инструкцию: `usermod -a -G systemd-journal crmbot`.
    """
    cmd = [
        "journalctl", "-u", _JOURNAL_UNIT,
        "--since", f"{hours} hours ago",
        "--no-pager", "-o", "short-iso", "-n", "2000",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except FileNotFoundError:
        return "[journalctl недоступен — не systemd-окружение]"
    except asyncio.TimeoutError:
        return "[journalctl превысил тайм-аут 20с]"
    except Exception as e:
        return f"[journalctl ошибка: {e}]"

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", "replace").strip()
        return f"[journalctl rc={proc.returncode}: {err[:200]}]"

    text = stdout.decode("utf-8", "replace")

    # Оставляем только строки с уровнем error/warning/exception + контекст
    # и трим до лимита, чтобы не раздувать промпт. Берём хвост — самые свежие.
    keep: List[str] = []
    pattern = re.compile(r"\b(ERROR|WARNING|Traceback|Exception|exception|error|failed|FAILED|CRITICAL)\b")
    for line in text.splitlines():
        if pattern.search(line):
            keep.append(line)
    filtered = "\n".join(keep) if keep else text

    if len(filtered) > _JOURNAL_MAX_BYTES:
        filtered = "...(обрезано)...\n" + filtered[-_JOURNAL_MAX_BYTES:]
    return filtered


async def _fetch_failed_requests(hours: int, limit: int = _AUDIT_MAX_ROWS) -> Tuple[int, List[dict]]:
    """Return (total_requests, list of failed rows ordered by recency)."""
    total_row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM audit_log WHERE created_at >= datetime('now', ?)",
        (f"-{hours} hours",),
    )
    total = int(total_row["n"]) if total_row else 0

    rows = await db.fetch_all(
        "SELECT created_at, question, error, model_used, tools_called "
        "FROM audit_log "
        "WHERE created_at >= datetime('now', ?) "
        "AND error IS NOT NULL AND error != '' "
        "ORDER BY created_at DESC LIMIT ?",
        (f"-{hours} hours", limit),
    )
    failed = [dict(r) for r in rows]
    return total, failed


def _format_failures_for_llm(failed: List[dict]) -> str:
    """Compact one-line-per-failure rendering for the LLM context."""
    if not failed:
        return "(нет записей с ошибками)"
    out: List[str] = []
    for r in failed:
        q = (r.get("question") or "").strip().replace("\n", " ")[:200]
        err = (r.get("error") or "").strip().replace("\n", " ")[:300]
        model = r.get("model_used") or "?"
        tools = r.get("tools_called") or ""
        out.append(
            f"[{r['created_at']}] model={model} tools={tools}\n"
            f"  Q: «{q}»\n  ERR: {err}"
        )
    return "\n".join(out)


_DIAGNOSIS_SYSTEM_PROMPT = """Ты — инженер-отладчик Telegram-бота для CRM Bitrix24. Бот написан
на Python (aiogram 3, aiohttp, aiosqlite, APScheduler), LLM — DeepSeek
V4-Flash через прямой API. Деплой — Timeweb (Ubuntu, systemd `crm-bot`,
пользователь crmbot, /opt/crm-bot).

Тебе на вход дают:
1) Сводку запросов из audit_log за сутки (поле error — питон-исключение
   или текст из ловушки).
2) Отфильтрованный хвост `journalctl -u crm-bot` за те же сутки.

Сформируй компактный HTML-разбор для админского чата Telegram. Правила:

— Используй ТОЛЬКО Telegram-HTML: <b>, <i>, <code>, <pre>. Никакого
  markdown (**, ##, ```). Эмодзи допустимы, не злоупотребляй.
— Сначала однострочная сводка: всего запросов / упало / процент.
— Если ошибок нет — короткое подтверждение, и всё.
— Иначе сгруппируй ошибки по реальной первопричине (НЕ по тексту
  исключения — связывай похожие в одну группу: «таймауты Bitrix»,
  «лимит итераций DeepSeek», «пустой ответ LLM», «отвалилась телефония»
  и т.п.).
— Для каждой группы: <b>Название</b> — N случаев. Одна строка пример
  («…», обрезай). Затем <i>Причина:</i> 1-2 предложения. Затем
  <i>Что делать:</i> конкретное действие (изменить код такого-то модуля,
  поднять лимит, добавить ретрай, проверить вебхук…). Будь предельно
  конкретен — если можешь назвать файл, скажи. Если рекомендация только
  на словах — так и пиши.
— В конце, если из journalctl всплывают системные проблемы (OOM,
  segfault, рестарты сервиса, утечки соединений), отдельным блоком
  <b>⚠ Системное</b> — что заметил.
— Длина — не больше 3500 символов суммарно. Не повторяйся.
— Не выдумывай данные. Если чего-то нет в логе/аудите — не упоминай.
"""


async def _call_deepseek(system_prompt: str, user_content: str, timeout: int = 90) -> str:
    """One-shot DeepSeek text completion. Returns raw text (or raises)."""
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content[:80_000]},
        ],
        "thinking": {"type": "disabled"},
        "temperature": 0.3,
    }
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    to = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=to) as session:
        async with session.post(url, json=body, headers=headers) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"DeepSeek {resp.status}: {text[:300]}")
            data = json.loads(text)
    return data["choices"][0]["message"]["content"]


async def build_error_diagnosis(hours: int = 24) -> str:
    """Return the full HTML diagnosis (header + AI body)."""
    total, failed = await _fetch_failed_requests(hours)
    header = f"🛠 <b>Авторазбор бота за {hours}ч</b>\n"

    if total == 0:
        return header + "\nЗа сутки запросов не было — анализ пропущен."

    if not failed:
        return (
            header
            + f"\nЗапросов: <b>{total}</b>\n✅ Ошибок не зафиксировано."
        )

    if not settings.deepseek_api_key:
        # Без LLM-ключа возвращаем хотя бы factual-дайджест, чтобы админ не остался без сигнала.
        fallback = await build_error_digest(hours=hours)
        return header + "\n<i>DeepSeek ключ не задан — fallback на факт-сводку:</i>\n\n" + fallback

    journal = await _read_journalctl(hours)

    pct = round(len(failed) / total * 100, 1) if total else 0
    user_content = (
        f"СУТОЧНАЯ СВОДКА: всего запросов {total}, упало {len(failed)} ({pct}%).\n\n"
        f"ПОСЛЕДНИЕ {len(failed)} ОШИБОК ИЗ audit_log:\n"
        f"{_format_failures_for_llm(failed)}\n\n"
        f"ЖУРНАЛ systemd (journalctl -u crm-bot, отфильтровано по ошибкам/предупреждениям):\n"
        f"{journal}"
    )

    try:
        body = await _call_deepseek(_DIAGNOSIS_SYSTEM_PROMPT, user_content)
    except Exception as e:
        logger.error("DeepSeek diagnosis call failed", error=str(e))
        fallback = await build_error_digest(hours=hours)
        return (
            header
            + f"\n⚠ DeepSeek не ответил ({e}). Падаю на факт-сводку:\n\n"
            + fallback
        )

    body = body.strip()
    full = header + "\n" + body
    if len(full) > _TG_MSG_LIMIT:
        full = full[:_TG_MSG_LIMIT] + "\n…(обрезано)"
    return full


async def send_error_diagnosis(bot: Bot, hours: int = 24) -> None:
    """Build AI diagnosis and DM it to the admin."""
    if not settings.admin_telegram_id:
        logger.warning("No admin_telegram_id — skipping error diagnosis")
        return
    try:
        text = await build_error_diagnosis(hours=hours)
        await bot.send_message(
            settings.admin_telegram_id, text, parse_mode=ParseMode.HTML
        )
        logger.info("Error diagnosis sent", hours=hours, chars=len(text))
    except Exception as e:
        logger.error("Failed to send error diagnosis", error=str(e))
