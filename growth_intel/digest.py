"""Композиция воронки + триггеров + упущенной выручки в один AI-отчёт.

Возвращает готовый Telegram-HTML текст «где у нас слабые места и куда
менеджерам приложить руки прямо сейчас». Используется:
  • on-demand через tool growth_opportunities в боте.
  • cron-джобом раз в неделю (понедельник утром) → чат РОПа.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import structlog

from b24.client import Bitrix24Client
from config import settings
from growth_intel.funnel import build_funnel
from growth_intel.missed import missed_revenue_summary
from growth_intel.triggers import TRIGGER_CATEGORIES, analyze_deal
from sales_comms.collector import iter_active_deals

logger = structlog.get_logger()


# ---------- запуск анализатора триггеров по живым сделкам ----------------

async def _deals_with_recent_activity(since_hours: int) -> set:
    """ID-шники сделок, по которым были коммуникации за последние N часов.
    Источник — локальная deal_communications (наполняется часовым sync).
    Сильно сокращает кандидатов для DeepSeek-анализа в ежедневном
    отчёте: обычно 15-30 сделок вместо 100."""
    from db.connection import db
    rows = await db.fetch_all(
        f"""
        SELECT DISTINCT deal_id
        FROM deal_communications
        WHERE occurred_at >= datetime('now', '-{int(since_hours)} hours')
          AND deal_id IS NOT NULL
        """,
    )
    # sqlite3.Row не поддерживает .get() — обращаемся через индексацию.
    return {int(r["deal_id"]) for r in rows or []}


async def refresh_signals(
    client: Bitrix24Client,
    *,
    limit: int = 100,
    since_hours: Optional[int] = None,
) -> Dict[str, Any]:
    """Прогнать triggers.analyze_deal по живым сделкам.

    since_hours=None — полный прогон по всем активным (≈100 сделок,
    ~6 мин DeepSeek, ₽4/прогон). Подходит для ручного бэкфилла или
    еженедельного глубокого сканирования.

    since_hours=24/48 — инкремент: берём только сделки с новой
    активностью за последние N часов (обычно 15-30 в сутки). Сделки
    без движения уже разобраны прошлым прогоном — повторно гонять их
    бесполезно. Время и стоимость падают в 4-5 раз без потери качества.
    """
    deals = await iter_active_deals(client, max_items=limit)

    if since_hours is not None and since_hours > 0:
        active_ids = await _deals_with_recent_activity(since_hours)
        deals = [d for d in deals if int(d.get("ID") or 0) in active_ids]
        logger.info(
            "refresh_signals incremental mode",
            since_hours=since_hours, candidates=len(deals),
        )

    total_added = 0
    total_satisfied = 0
    processed = 0
    for d in deals:
        try:
            did = int(d["ID"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            opp = float(d.get("OPPORTUNITY") or 0)
        except (TypeError, ValueError):
            opp = 0.0
        mid = d.get("ASSIGNED_BY_ID")
        try:
            mid = int(mid) if mid is not None else None
        except (TypeError, ValueError):
            mid = None
        res = await analyze_deal(did, manager_id=mid, opportunity=opp)
        total_added += int(res.get("triggers_added") or 0)
        total_satisfied += int(res.get("satisfied_now") or 0)
        processed += 1
        await asyncio.sleep(0.1)   # лёгкий троттлинг, чтобы не упереться в DeepSeek rate
    return {
        "deals_scanned": processed,
        "new_triggers": total_added,
        "satisfied_now": total_satisfied,
    }


# ---------- сборка контекста для итогового LLM-обзора --------------------

def _format_funnel_block(funnel: Dict[str, Any]) -> str:
    lines = ["ВОРОНКА ПО МЕНЕДЖЕРАМ (за период)"]
    for name, m in funnel["managers"].items():
        if "error" in m:
            lines.append(f"  {name}: {m['error']}")
            continue
        b = m["buckets"]
        lines.append(
            f"  {name}: всего {m['total_deals']} сделок, "
            f"в работе {b.get('proposal',0)+b.get('measurement',0)+b.get('invoice',0)+b.get('created',0)}, "
            f"выиграно {b.get('won',0)} ({m['conversion_to_won_pct']}%), "
            f"проиграно {b.get('lost',0)}, выручка ₽{int(m['won_revenue']):,}".replace(",", " ")
        )
    tt = funnel["team_total"]
    lines.append(
        f"  ИТОГО ОТДЕЛ: {tt['total_deals']} сделок, "
        f"конверсия в продажу {tt['conversion_to_won_pct']}%, "
        f"выручка ₽{int(tt['won_revenue']):,}".replace(",", " ")
    )
    return "\n".join(lines)


def _format_signals_block(missed: Dict[str, Any], deal_titles: Dict[int, str], user_names: Dict[int, str]) -> str:
    lines = [
        f"НЕОТРАБОТАННЫЕ СИГНАЛЫ: {missed['total_signals']} шт, "
        f"под угрозой ₽{int(missed['total_at_risk_rub']):,} (high: ₽{int(missed['high_severity_at_risk_rub']):,})".replace(",", " ")
    ]
    if missed["by_category"]:
        cats = ", ".join(f"{k}={v}" for k, v in missed["by_category"].items())
        lines.append(f"  по категориям: {cats}")
    lines.append("")
    lines.append("ТОП сделок где висят сигналы (отсортированы по сумме):")
    for s in missed["top_signals"][:12]:
        did = s["deal_id"]
        title = deal_titles.get(did, "")[:40]
        mgr = user_names.get(s.get("manager_id"), "?")
        opp = int(s.get("value_at_risk") or 0)
        when = (s.get("detected_at") or "")[:10]
        cat = s.get("category", "?")
        lines.append(
            f"  #{did} «{title}» — ₽{opp:,} — {mgr} — {cat} ({when})\n     {s.get('evidence','')[:200]}".replace(",", " ")
        )
    return "\n".join(lines)


async def _enrich_top_signals(
    client: Bitrix24Client, missed: Dict[str, Any]
) -> Dict[int, str]:
    """Подтянуть TITLE для топ-сделок (нужно для читаемого дайджеста)."""
    ids = sorted({s["deal_id"] for s in missed.get("top_signals", [])})
    if not ids:
        return {}
    titles: Dict[int, str] = {}
    # Bulk fetch через filter ID
    items, _ = await client._paginate(
        "crm.deal.list",
        params={
            "filter": {"ID": ids},
            "select": ["ID", "TITLE"],
        },
        max_items=len(ids),
    )
    for d in items or []:
        try:
            titles[int(d["ID"])] = d.get("TITLE") or ""
        except (KeyError, ValueError):
            pass
    return titles


async def _fetch_comms_for_top(missed: Dict[str, Any], per_deal: int = 8) -> Dict[int, List[Dict[str, Any]]]:
    """Для каждой топ-сделки достать последние коммуникации из sales_comms.
    Используется в детальном HTML-отчёте: цитаты из звонков и переписки.
    """
    from sales_comms.db import communications_for_deal
    deal_ids = sorted({int(s["deal_id"]) for s in missed.get("top_signals", [])})
    out: Dict[int, List[Dict[str, Any]]] = {}
    for did in deal_ids[:12]:   # cap чтобы не перегрузить промпт
        try:
            comms = await communications_for_deal(did, max_items=per_deal)
            # сортируем хронологически (старое → новое) — LLM лучше читает
            comms.sort(key=lambda c: c.get("occurred_at") or "")
            out[did] = comms
        except Exception as e:
            logger.warning("fetch comms for deal failed", deal_id=did, error=str(e))
    return out


def _format_comms_for_signal(
    deal_id: int,
    comms: List[Dict[str, Any]],
    char_cap: int = 1800,
) -> str:
    """Превратить коммуникации одной сделки в компактный текст для DeepSeek."""
    if not comms:
        return ""
    lines = []
    for c in comms:
        ts = (c.get("occurred_at") or "")[:16].replace("T", " ")
        src = c.get("source_type", "?")
        direction = c.get("direction") or ""
        author = c.get("author_name") or ("клиент" if direction == "in" else "?")
        text = (c.get("text") or "").strip().replace("\n", " ")
        if not text and src == "call":
            dur = c.get("duration_sec") or 0
            lines.append(f"  [{ts}] звонок {dur}с (без расшифровки)")
            continue
        if not text:
            continue
        if src == "call":
            lines.append(f"  [{ts}] звонок: «{text[:500]}»")
        elif src == "openline":
            who = "МЕНЕДЖЕР" if direction == "out" else "КЛИЕНТ"
            lines.append(f"  [{ts}] {who}: «{text[:400]}»")
        elif src == "comment":
            lines.append(f"  [{ts}] коммент менеджера: «{text[:300]}»")
        elif src == "email":
            sub = c.get("subject") or "письмо"
            lines.append(f"  [{ts}] EMAIL «{sub}»: «{text[:200]}»")
        else:
            lines.append(f"  [{ts}] {src}: «{text[:200]}»")
    out = "\n".join(lines)
    return out[:char_cap]


# ---------- финальный LLM-проход — текст отчёта --------------------------

_SYSTEM_PROMPT_DETAILED = """Ты — старший аналитик отдела продаж Growzone.
Тебе дают данные:
  1) воронка по 3 менеджерам (Шеян, Ребров, Останина)
  2) топ горящих сделок (триггеры) с суммами под риском
  3) ДЛЯ КАЖДОЙ топ-сделки — выписку из переписки и расшифровок звонков
     (хронологически, старое → свежее)

Сформируй JSON-ответ строго в этом формате:
{
  "short_summary_html": "<b>...</b> ≤1800 символов, Telegram-HTML, для chat-сообщения",
  "detailed_html_body": "...без обёртки <html>, только содержимое <body>, до 30000 символов"
}

ВАЖНОЕ О ТЕРМИНОЛОГИИ:
— У Growzone «продажа», «выручка», «закрытая сделка» = сделка перешла
   на стадию «Договор заключён, внесён аванс» (STAGE_ID=PREPARATION).
   В воронке поле «выиграно» — это уже коммерческие продажи.
— НЕ ПИШИ «отдел не закрыл ни одной сделки» если в данных won>0.

═══ short_summary_html ═══
Краткая сводка для Telegram, ≤1800 символов:
  • Главная цифра выручки и под угрозой (1-2 строки)
  • Топ-3 самых горящих сделок одной строкой каждая: #ID, клиент,
    сумма, менеджер, действие в одну фразу
  • Внизу: 📎 «Подробный разбор каждой сделки — во вложенном файле»
Только теги <b>, <i>. Без <br/>, <p>, <div>.

═══ detailed_html_body ═══
Развёрнутый HTML-разбор для отдельной страницы:

<h1>📊 Утренний разбор продаж — {{date}}</h1>

<section>
  <h2>1. Главное</h2>
  Цифры выручки за период, под угрозой, динамика.
</section>

<section>
  <h2>2. Топ сделок, где нужно дожимать</h2>
  Для каждой топ-сделки (5-10 шт) — отдельный блок:

  <article class="deal">
    <h3>🔥 #18524 — Руслан, Бурцево МСК — ₽3 727 164</h3>
    <p class="manager">Менеджер: Ребров Никита · Категория: client_ready_to_pay</p>

    <h4>📋 Что произошло (по переписке и звонкам):</h4>
    <ul>
      <li><time>21.05 14:30</time> Клиент в WhatsApp: <q>Готовы заключаться, давайте счёт</q></li>
      <li><time>22.05 11:00</time> Звонок Реброва (4 мин): обсудили техзадание, договорились что счёт «завтра»</li>
      <li><time>23.05</time> — счёт не выставлен, тишина</li>
    </ul>

    <h4>🎯 Что сказать менеджеру:</h4>
    <ol>
      <li>Позвонить Руслану ПРЯМО СЕЙЧАС</li>
      <li>Извиниться за задержку</li>
      <li>Уточнить реквизиты (ИП или ООО?)</li>
      <li>Сказать: «Счёт за 30 минут, аванс ₽1.5M ждём в среду»</li>
    </ol>

    <p class="risk">⚠️ Под угрозой: <b>₽3.7M</b>. Если до пятницы не закроем — клиент уйдёт к конкуренту.</p>
  </article>

  (повторить для остальных топ-сделок)
</section>

<section>
  <h2>3. Воронка по менеджерам</h2>
  Для каждого: сколько сделок, продано, проиграно, конверсия,
  гипотеза где проседает (со ссылкой на конкретные сделки если из
  триггеров видно).
</section>

<section>
  <h2>4. Рекомендации на сегодня</h2>
  3-5 конкретных действий для РОПа.
</section>

ВАЖНО:
— ID сделок ВСЕГДА оформляй как <a href="#{{ID}}">#NNNNN</a> — потом
   их превратят в кликабельные ссылки на Bitrix.
— Цитируй реальный текст из переписки! Если в данных есть «оплачу
   завтра» — так и пиши, в кавычках.
— Если для какой-то топ-сделки нет коммуникаций — пиши «Нет данных
   из переписки, рекомендую открыть карточку и посмотреть лично».
— Не используй <br/>, <div>, <span class>. Только семантические
   теги: h1-h4, p, ul/ol/li, q, blockquote, time, b, i, a, section,
   article.
— Russian. Длина detailed_html_body ≤30000 символов.
"""

_SYSTEM_PROMPT = """Ты — старший аналитик отдела продаж компании Growzone.
Тебе дают сырые цифры и факты по работе 3 менеджеров (Шеян Андрей,
Ребров Никита, Останина Любовь):
  1) воронка конверсии по менеджерам за период
  2) список неотработанных триггеров (где клиент дозрел, а менеджер
     ничего не сделал), с суммами сделок под риском
  3) топ конкретных сделок где скорее всего теряем деньги

Сформируй отчёт для руководителя отдела продаж в Telegram-HTML формате
(<b>...</b>, <i>...</i>). Структура:

<b>💰 Где растут деньги, где теряются — отчёт за период</b>

<b>1. Главное</b>
— одна-две фразы про общую картину: выручка/конверсия, динамика.
— цифра «упущенная выручка ₽X» (из total_at_risk).

<b>2. Топ-N сделок, где сейчас НАДО ДОЖИМАТЬ</b>
По 3-5 самых дорогих горящих сделок: ID, клиент, менеджер, что
конкретно сделать (счёт выставить / реквизиты ждём / перезвонить),
дедлайн.

<b>3. Где у каждого менеджера течёт воронка</b>
По каждому из 3 менеджеров: где конкретно проседает (стадия) и
гипотеза почему (если из триггеров видно — отрабатывает возражения
плохо? медленно отвечает? не предлагает следующий шаг?).

<b>4. Рекомендации на неделю</b>
3-5 конкретных действий для РОПа (не общих советов, а «Шеяну —
проконтролировать X», «Реброву — связаться с Y»).

⚠️ ВАЖНО О ТЕРМИНОЛОГИИ:
— У Growzone «продажа», «выручка», «закрытая сделка» = сделка перешла
   на стадию «Договор заключён, внесён аванс» (STAGE_ID=PREPARATION).
   В цифрах воронки это поле «выиграно» — это уже коммерческие продажи
   (договор+аванс), а не пустая финальная стадия.
— Если в воронке стоит «выиграно: N, выручка ₽X» — это N заключённых
   договоров и сумма авансов/договоров. ИХ И НАЗЫВАЙ продажами.
— НЕ ПИШИ «отдел продаж не закрыл ни одной сделки», если в данных
   есть won > 0.

Правила:
— Не выдумывай. Если в данных нет цифр — не пиши «конверсия упала».
— Russian. Telegram-HTML. Длина ≤3500 символов.
— Указывай ID сделок чтобы РОП их быстро находил.
"""


async def _call_deepseek_digest(context: str) -> str:
    if not settings.deepseek_api_key:
        return "⚠️ DeepSeek API не настроен — отчёт не собрать."
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "Сырые данные по отделу продаж:\n\n" + context[:40_000]},
        ],
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
        "max_tokens": 2500,
    }
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=240)) as s:
            async with s.post(url, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error("Growth digest LLM error", status=resp.status, body=text[:300])
                    return f"⚠️ DeepSeek вернул {resp.status}: {text[:200]}"
                data = json.loads(text)
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error("Growth digest LLM failed", error=str(e))
        return f"⚠️ Не удалось получить ответ от DeepSeek: {e}"


async def _call_deepseek_detailed(context: str) -> Dict[str, str]:
    """Подробный JSON-ответ DeepSeek для HTML-разбора:
       {short_summary_html, detailed_html_body}.
    Если что-то не так — возвращаем dict с error-ключом, наверху
    fallback на старый _call_deepseek_digest.
    """
    if not settings.deepseek_api_key:
        return {"error": "DeepSeek API не настроен"}
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT_DETAILED},
            {"role": "user", "content": "Данные по отделу продаж за период:\n\n" + context[:80_000]},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.2,
        "max_tokens": 8000,
    }
    url = settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=360)) as s:
            async with s.post(url, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.error("Detailed digest LLM error", status=resp.status, body=text[:300])
                    return {"error": f"DeepSeek {resp.status}: {text[:200]}"}
                data = json.loads(text)
        content = data["choices"][0]["message"]["content"].strip()
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return {"error": "DeepSeek вернул не объект"}
        return {
            "short": str(parsed.get("short_summary_html") or "").strip(),
            "detailed": str(parsed.get("detailed_html_body") or "").strip(),
        }
    except Exception as e:
        logger.error("Detailed digest LLM failed", error=str(e))
        return {"error": str(e)}


# ---------- HTML-обёртка для аттача ---------------------------------------

_PORTAL_URL_FALLBACK = "https://growzone.bitrix24.ru"
_DEAL_ID_RE = re.compile(r"#(\d{4,6})")


def _linkify_deal_ids(html: str, portal_url: str) -> str:
    """Превратить #18524 в <a href="...">#18524</a> — кликабельные ссылки
    на карточки сделок в Bitrix24. Не трогает уже завёрнутые в <a> ID."""
    if not html:
        return ""
    portal = (portal_url or _PORTAL_URL_FALLBACK).rstrip("/")

    def _replace(m: re.Match) -> str:
        # Если уже есть href рядом — не трогаем (грубо проверяем по контексту).
        # Полностью обернуть в <a> простым regex'ом — нормально, дубликат
        # <a><a>#…</a></a> не появится потому что Re не идёт по уже-замене.
        did = m.group(1)
        return f'<a href="{portal}/crm/deal/details/{did}/">#{did}</a>'

    return _DEAL_ID_RE.sub(_replace, html)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Утренний разбор продаж — {date}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --primary: #1a8754;
    --danger: #c92a2a;
    --warn: #b88300;
    --bg: #f8f9fb;
    --card: #fff;
    --line: #e6e8eb;
    --text: #1a1d21;
    --muted: #5a6470;
  }}
  body {{
    margin: 0;
    padding: 16px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 16px;
    line-height: 1.55;
    -webkit-text-size-adjust: 100%;
  }}
  .container {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 26px; margin: 8px 0 4px; }}
  .meta {{ color: var(--muted); font-size: 14px; margin-bottom: 24px; }}
  section {{
    background: var(--card); border: 1px solid var(--line);
    border-radius: 12px; padding: 18px 22px; margin: 14px 0;
  }}
  section h2 {{ font-size: 20px; margin: 0 0 12px; }}
  article.deal {{
    border-left: 4px solid var(--danger); padding-left: 16px;
    margin: 22px 0;
  }}
  article.deal h3 {{ font-size: 18px; margin: 0 0 4px; }}
  article.deal .manager {{ color: var(--muted); font-size: 14px; margin: 0 0 12px; }}
  article.deal h4 {{ font-size: 15px; margin: 14px 0 6px; color: var(--muted); }}
  article.deal q {{
    background: #fff4e5; padding: 1px 6px; border-radius: 4px;
    font-style: italic; color: #5a3500;
  }}
  article.deal time {{ color: var(--muted); font-weight: 500; margin-right: 6px; }}
  article.deal .risk {{
    background: #fff4f4; border-left: 3px solid var(--danger);
    padding: 8px 12px; margin: 12px 0 0; border-radius: 0 6px 6px 0;
  }}
  ul, ol {{ padding-left: 22px; margin: 6px 0; }}
  li {{ margin: 4px 0; }}
  a {{ color: #0066d6; text-decoration: none; border-bottom: 1px solid #cfe0f5; }}
  a:hover {{ border-color: #0066d6; }}
  .footer {{
    margin: 32px 0 8px; color: var(--muted); font-size: 13px;
    text-align: center;
  }}
</style>
</head>
<body>
<div class="container">
{body}
<div class="footer">Сгенерировано ботом «Гроу» · {date} {time}</div>
</div>
</body>
</html>
"""


def render_html_page(detailed_body: str, *, date: str, portal_url: str = "") -> str:
    """Обернуть detailed_html_body из DeepSeek в полноценную HTML-страницу
    с CSS, linkify ID сделок."""
    from datetime import datetime as _dt
    body_html = _linkify_deal_ids(detailed_body, portal_url)
    return _HTML_TEMPLATE.format(
        date=date, time=_dt.now().strftime("%H:%M"), body=body_html,
    )


# ---------- публичная точка входа -----------------------------------------

async def build_growth_digest(
    *,
    client: Optional[Bitrix24Client] = None,
    period_days: int = 30,
    skip_refresh: bool = False,
    refresh_limit: int = 60,
    refresh_since_hours: Optional[int] = None,
) -> Dict[str, Any]:
    """Собрать отчёт «где растут деньги, где теряются».

    skip_refresh=True — не запускать analyze_deal перед сборкой
    (читать то что есть в growth_signals). Для on-demand вызовов из
    бота, чтобы не ждать 5-7 минут DeepSeek-проходов.

    refresh_since_hours=24 — инкрементальный режим: проходим только по
    сделкам с активностью за сутки. Используется ежедневным cron'ом —
    в 4-5 раз быстрее и дешевле полного скана.
    """
    own_client = client is None
    if own_client:
        client = Bitrix24Client()
    try:
        if not skip_refresh:
            logger.info("Growth digest: refreshing signals",
                        limit=refresh_limit, since_hours=refresh_since_hours)
            refresh = await refresh_signals(
                client, limit=refresh_limit, since_hours=refresh_since_hours,
            )
            logger.info("Growth digest: signals refreshed", **refresh)

        funnel = await build_funnel(
            client,
            date_from=date.today() - timedelta(days=period_days),
            date_to=date.today(),
        )
        missed = await missed_revenue_summary()
        deal_titles = await _enrich_top_signals(client, missed)
        users_map = await client.get_users_map()
        user_names = {uid: info.get("name", str(uid)) for uid, info in users_map.items()}

        # Расширенный контекст: для топ-сделок добавляем выписки из
        # переписки (DeepSeek сможет цитировать «оплачу завтра» и т.п.).
        comms_by_deal = await _fetch_comms_for_top(missed, per_deal=8)
        signals_block = _format_signals_block(missed, deal_titles, user_names)
        comms_block_lines = ["", "ВЫПИСКИ ИЗ ПЕРЕПИСКИ ПО ТОП-СДЕЛКАМ:"]
        for did, comms in comms_by_deal.items():
            title = deal_titles.get(did, "")[:50]
            comms_text = _format_comms_for_signal(did, comms)
            if not comms_text:
                comms_text = "  (нет коммуникаций в БД — посмотреть карточку вручную)"
            comms_block_lines.append(f"\nСделка #{did} «{title}»:\n{comms_text}")
        comms_block = "\n".join(comms_block_lines)

        context = (
            _format_funnel_block(funnel)
            + "\n\n"
            + signals_block
            + "\n\n"
            + comms_block
        )

        # Пробуем сначала детальный JSON-режим (для HTML-аттача).
        # Fallback на старый одношаговый narrative если что-то пойдёт не так.
        detailed = await _call_deepseek_detailed(context)
        short_text: str = ""
        html_body: str = ""
        if "error" in detailed:
            logger.warning("Detailed digest failed, falling back to single-text",
                           error=detailed["error"])
            narrative = await _call_deepseek_digest(context)
            short_text = narrative
        else:
            short_text = detailed.get("short", "")
            html_body = detailed.get("detailed", "")

        return {
            "text": short_text,
            "html_body": html_body,
            "html_page": (
                render_html_page(html_body, date=date.today().isoformat(),
                                 portal_url=settings.b24_portal_url)
                if html_body else ""
            ),
            "total_at_risk_rub": missed["total_at_risk_rub"],
            "signals_count": missed["total_signals"],
            "deals_in_funnel": funnel["team_total"]["total_deals"],
            "won_revenue": funnel["team_total"]["won_revenue"],
        }
    finally:
        if own_client:
            try:
                await client.close()
            except Exception:
                pass
