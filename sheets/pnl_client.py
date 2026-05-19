"""Read-only client for the 'ОПиУ PnL Growzone Москва' Google Sheet.

Парсит помесячные листы '🔹 MM.YYYY' (одна структура с 05.2023 по текущий месяц),
агрегирует по всем годам в один большой словарь { статья: { 'YYYY-MM': число } }.

Структура листа:
- col A: 'Источники' (метка)
- col B: название статьи (Выручка, Монтаж, Производственные расходы, ...)
- col C: Москва (для 2025+) или Growzone (для 2023-2024)
- col D: Казань (только 2025+)
- col E: Итого (только 2025+)
- col B со словами 'EBITDA', 'Маржинальный', 'Валовая', 'Чистая прибыль' — итоги

Берём всегда колонку «Итого»: E для 2025+, C для 2023-2024.
"""

import asyncio
import re
import time
import structlog
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials

from config import settings

logger = structlog.get_logger()


_MONTH_RE = re.compile(r"(\d{2})\.(\d{4})")


def _money(s) -> Optional[float]:
    """Parse 'р.5 600 398', '5,600,398', '-р.474 174', '54%' (→ None) → float.
    Returns None for percentages and garbage.
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or s == "-":
        return None
    if "%" in s:
        return None
    if "REF" in s or "DIV" in s or "#" in s:
        return None
    s = s.replace("\xa0", "").replace(" ", "")
    s = s.replace("р.", "").replace("руб", "").replace("₽", "")
    # 1 234,56 → 1234.56; but 5,600,398 (comma as thousand sep) → 5600398
    if s.count(",") > 1:
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    if not s or s in (".", "-", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _percent(s) -> Optional[float]:
    """Parse '54%' → 54.0. Returns None for non-percent values."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s or "%" not in s:
        return None
    s = s.replace("\xa0", "").replace(" ", "").replace("%", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# Статьи, для которых вместо суммы хотим хранить % — это рентабельности.
_PERCENT_ARTICLES = {
    "Рентабельность по маржинальному",
    "Рентабельность по маржинальному доходу",
    "Рентабельность по ВП, %",
    "Рентабельность по ВП",
    "Рентабельность по операционной прибыли (EBITDA)",
    "Рентабельность по операционной прибыли",
    "Рентабельность по операционной",
    "Рентабельность по чистой прибыли",
}

# Каноническое имя → список синонимов (как могут называться в листах разных лет).
# Используется чтобы свести разнобой имён к единому ключу.
_ARTICLE_ALIASES = {
    "Покупка оборудования для поливов": ["Покупка оборудования"],
    "Оплата рабочим по поливам": ["Оплата рабочим"],
    "Подрядчики в рамках сделки": ["Оплата подрядчикам в рамках сделки"],
}


def _canonical_article(name: str) -> str:
    """Привести имя статьи к каноническому виду (схлопывает синонимы)."""
    name = (name or "").strip()
    for canonical, aliases in _ARTICLE_ALIASES.items():
        if name == canonical or name in aliases:
            return canonical
    return name


# Группировка статей маркетинга (для расчёта 'всего маркетинг' и ДРР).
# Это статьи коммерческого блока, относящиеся к маркетингу.
_MARKETING_ARTICLES = {
    "Яндекс", "Авито", "ГЦК идентификации", "Пиксель идентификации",
    "ВК", "Прочие маркетинговые расходы", "Услуги субподрядчиков по маркетингу",
    "Директолог", "Авитологи", "КЦ ГЦК", "Прочие", "СЕО",
    "Рекламный бюджет на маркетинг", "Авито Медиа", "Авито медиа",
}


class PnLSheetClient:
    """Читает все помесячные листы ОПиУ. Кэш 30 минут."""

    SPREADSHEET_ID = "1gneiD29_XPWLqakM1lW-rCwAM-fPkxSvm2QxpuxYIbY"
    SHEET_PREFIX = "🔹"

    def __init__(self):
        self._gc: Optional[gspread.Client] = None
        # _all: { article: { 'YYYY-MM': float } }
        self._all: Optional[Dict[str, Dict[str, float]]] = None
        # _percents: { article: { 'YYYY-MM': float } } — рентабельности отдельно
        self._percents: Optional[Dict[str, Dict[str, float]]] = None
        # _by_month: { 'YYYY-MM': { article: {'Москва':..., 'Казань':..., 'Итого':...} } }
        self._by_month: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None
        self._cache_expires: float = 0.0
        self.cache_ttl_sec = 1800  # 30 min
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(settings.google_sa_path and Path(settings.google_sa_path).exists())

    def _client(self) -> gspread.Client:
        if self._gc is None:
            creds = Credentials.from_service_account_file(
                settings.google_sa_path,
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets.readonly",
                    "https://www.googleapis.com/auth/drive.readonly",
                ],
            )
            self._gc = gspread.authorize(creds)
        return self._gc

    def _parse_sheet_name(self, name: str) -> Optional[str]:
        """'🔹 04.2026' → '2026-04'. Возвращает None если не помесячный лист."""
        m = _MONTH_RE.search(name)
        if not m:
            return None
        mm, yyyy = m.group(1), m.group(2)
        return f"{yyyy}-{mm}"

    def _parse_month_rows(self, rows: List[List[str]], ym: str) -> Tuple[
        Dict[str, float],  # totals (Итого) by article
        Dict[str, float],  # percents by article
        Dict[str, Dict[str, float]],  # detail by article: {Москва, Казань, Итого}
    ]:
        """Распарсить строки одного помесячного листа.

        Логика выбора колонки 'Итого':
        - В 2025+ есть Москва/Казань/Итого → col индексы 2/3/4 (0-based)
        - В 2023-2024 одна колонка Growzone → col index 2 = 'Итого'
        Определяем по первой строке.
        """
        if not rows:
            return {}, {}, {}

        header = rows[0] if rows else []
        # Проверяем какие колонки есть: ищем 'Москва'/'Казань'/'Итого' в R1
        has_branches = False
        moscow_idx, kazan_idx, total_idx = None, None, 2  # default — col C
        for i, cell in enumerate(header[:8]):
            cell = (cell or "").strip()
            if cell == "Москва":
                moscow_idx = i
                has_branches = True
            elif cell == "Казань":
                kazan_idx = i
            elif cell == "Итого":
                total_idx = i
                has_branches = True

        # Если ничего не нашли — лист в старом формате (только Growzone в col C)
        if not has_branches:
            total_idx = 2
            moscow_idx, kazan_idx = None, None

        totals: Dict[str, float] = {}
        percents: Dict[str, float] = {}
        detail: Dict[str, Dict[str, float]] = {}

        for r in rows[1:]:
            # Строки могут быть короче total_idx если в листе ячейки 'Итого' пусты
            # (Google API не возвращает trailing empty cells). Дополним пустыми.
            if len(r) < total_idx + 1:
                r = list(r) + [""] * (total_idx + 1 - len(r))
            article_raw = (r[1] if len(r) > 1 else "").strip()
            if not article_raw:
                continue
            article = _canonical_article(article_raw)

            # Это процент (рентабельность)?
            is_pct = any(p.lower() in article.lower() for p in (
                "рентабельность", "маржинальность %",
            ))

            total_cell = r[total_idx] if total_idx < len(r) else ""

            if is_pct:
                pv = _percent(total_cell)
                if pv is not None:
                    percents[article] = pv
            else:
                v = _money(total_cell)
                # Финальные итоговые строки ('Чистая прибыль Итого' / 'Чистая прибыль'
                # / 'Рентабельность по...') иногда лежат только в col C — без копии
                # в col 'Итого'. Если в Итого пусто или 0, фолбэкаемся на col C.
                if (v is None or v == 0) and has_branches and moscow_idx is not None:
                    fb = _money(r[moscow_idx]) if moscow_idx < len(r) else None
                    if fb is not None:
                        v = fb
                if v is not None:
                    # Если статья встречается несколько раз в листе (дубли названий
                    # типа 'ВК' в коммерческом блоке встречаются дважды) —
                    # суммируем.
                    totals[article] = totals.get(article, 0.0) + v

            # Детализация Москва/Казань — только если есть филиальные колонки
            if has_branches and moscow_idx is not None and not is_pct:
                m_val = _money(r[moscow_idx]) if moscow_idx < len(r) else None
                k_val = _money(r[kazan_idx]) if kazan_idx is not None and kazan_idx < len(r) else None
                t_val = _money(total_cell)
                if m_val is not None or k_val is not None or t_val is not None:
                    d = detail.setdefault(article, {"Москва": 0.0, "Казань": 0.0, "Итого": 0.0})
                    if m_val is not None:
                        d["Москва"] += m_val
                    if k_val is not None:
                        d["Казань"] += k_val
                    if t_val is not None:
                        d["Итого"] += t_val

        return totals, percents, detail

    async def _load(self) -> Tuple[
        Dict[str, Dict[str, float]],
        Dict[str, Dict[str, float]],
        Dict[str, Dict[str, Dict[str, float]]],
    ]:
        """Загрузить все помесячные листы. Кэш 30 мин.

        Returns (all_totals, all_percents, by_month_detail).
        """
        async with self._lock:
            now = time.time()
            if (
                self._all is not None
                and self._percents is not None
                and self._by_month is not None
                and self._cache_expires > now
            ):
                return self._all, self._percents, self._by_month

            def sync_fetch():
                sh = self._client().open_by_key(self.SPREADSHEET_ID)
                worksheets = sh.worksheets()
                month_sheets: List[Tuple[str, gspread.Worksheet]] = []
                for ws in worksheets:
                    title = ws.title
                    if self.SHEET_PREFIX not in title:
                        continue
                    ym = self._parse_sheet_name(title)
                    if ym:
                        month_sheets.append((ym, ws))
                # Sort by ym
                month_sheets.sort(key=lambda x: x[0])
                # Bulk-fetch values for each — используем batch_get для скорости
                spreadsheet = sh
                ranges = [f"'{ws.title}'!A1:G100" for _, ws in month_sheets]
                # gspread spreadsheet.values_batch_get expects ranges param
                resp = spreadsheet.values_batch_get(ranges)
                value_ranges = resp.get("valueRanges", [])
                result = []
                for (ym, ws), vr in zip(month_sheets, value_ranges):
                    rows = vr.get("values", [])
                    result.append((ym, rows))
                return result

            month_data = await asyncio.to_thread(sync_fetch)

            all_totals: Dict[str, Dict[str, float]] = {}
            all_percents: Dict[str, Dict[str, float]] = {}
            by_month: Dict[str, Dict[str, Dict[str, float]]] = {}

            for ym, rows in month_data:
                totals, percents, detail = self._parse_month_rows(rows, ym)
                for article, v in totals.items():
                    all_totals.setdefault(article, {})[ym] = v
                for article, v in percents.items():
                    all_percents.setdefault(article, {})[ym] = v
                if detail:
                    by_month[ym] = detail

            self._all = all_totals
            self._percents = all_percents
            self._by_month = by_month
            self._cache_expires = now + self.cache_ttl_sec
            logger.info(
                "PnL sheet loaded",
                months=len(month_data),
                articles=len(all_totals),
                cache_ttl_sec=self.cache_ttl_sec,
            )
            return all_totals, all_percents, by_month

    # ---------- public helpers ----------

    def _available_months(self, totals: Dict[str, Dict[str, float]]) -> List[str]:
        seen = set()
        for d in totals.values():
            seen.update(d.keys())
        return sorted(seen)

    def _get(self, totals: Dict[str, Dict[str, float]], article: str, ym: str) -> float:
        return float((totals.get(article) or {}).get(ym, 0.0) or 0.0)

    def _safe_pct(self, num: float, denom: float) -> Optional[float]:
        if not denom:
            return None
        return round(num / denom * 100, 1)

    # ---------- public API ----------

    async def summary(self, months: int = 6) -> Dict[str, Any]:
        """Последние N месяцев — компактная KPI-сводка для LLM-анализа.

        Возвращает по каждому месяцу: выручка, маржа, маржинальность,
        валовая прибыль, EBITDA, EBITDA-маржа, чистая прибыль, рент. ЧП,
        маркетинг (сумма коммерческих маркетинговых статей), ДРР %.
        """
        totals, percents, _ = await self._load()
        all_months = self._available_months(totals)
        if not all_months:
            return {"error": "Нет данных в таблице ОПиУ"}

        months = max(1, min(int(months or 6), 48))
        selected = all_months[-months:]

        def get(article: str, ym: str) -> float:
            return self._get(totals, article, ym)

        def pct_from_sheet(article: str, ym: str) -> Optional[float]:
            return (percents.get(article) or {}).get(ym)

        rows: List[Dict[str, Any]] = []
        for ym in selected:
            revenue = get("Выручка", ym)
            margin = get("Маржинальный доход", ym)
            gross = get("Валовая прибыль", ym)
            ebitda = get("Операционная прибыль (EBITDA)", ym)
            net = get("Чистая прибыль Итого", ym) or get("Чистая прибыль", ym)
            mkt = sum(get(a, ym) for a in _MARKETING_ARTICLES)
            # Берём готовые % из листа если есть, иначе считаем
            margin_pct = (
                pct_from_sheet("Рентабельность по маржинальному", ym)
                or pct_from_sheet("Рентабельность по маржинальному доходу", ym)
                or self._safe_pct(margin, revenue)
            )
            gross_pct = (
                pct_from_sheet("Рентабельность по ВП, %", ym)
                or pct_from_sheet("Рентабельность по ВП", ym)
                or self._safe_pct(gross, revenue)
            )
            ebitda_pct = (
                pct_from_sheet("Рентабельность по операционной прибыли (EBITDA)", ym)
                or pct_from_sheet("Рентабельность по операционной", ym)
                or self._safe_pct(ebitda, revenue)
            )
            net_pct = (
                pct_from_sheet("Рентабельность по чистой прибыли", ym)
                or self._safe_pct(net, revenue)
            )
            drr_pct = self._safe_pct(mkt, revenue)

            rows.append({
                "month": ym,
                "revenue": round(revenue),
                "margin": round(margin),
                "margin_pct": margin_pct,
                "gross_profit": round(gross),
                "gross_pct": gross_pct,
                "ebitda": round(ebitda),
                "ebitda_pct": ebitda_pct,
                "net_profit": round(net),
                "net_pct": net_pct,
                "marketing": round(mkt),
                "drr_pct": drr_pct,
            })

        return {
            "period": {"from": selected[0], "to": selected[-1], "months": len(selected)},
            "available_months_range": {
                "earliest": all_months[0],
                "latest": all_months[-1],
                "total_count": len(all_months),
            },
            "note": (
                "Данные из помесячных листов '🔹 MM.YYYY' таблицы ОПиУ. Колонка 'Итого' "
                "(в 2025+ это Москва+Казань). Маркетинг = сумма коммерческих "
                "маркетинговых статей (Яндекс, Авито, ВК, Директолог, КЦ ГЦК и т.д.). "
                "ДРР = маркетинг / выручка."
            ),
            "kpi_by_month": rows,
        }

    async def articles(
        self,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        articles: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Возвращает помесячные значения по выбранным статьям за период.

        date_from/date_to в формате 'YYYY-MM' (включительно). Если не указано —
        весь доступный диапазон.

        articles: список конкретных статей. Если None — все известные.
        """
        totals, percents, _ = await self._load()
        all_months = self._available_months(totals)
        if not all_months:
            return {"error": "Нет данных в таблице ОПиУ"}

        df = date_from or all_months[0]
        dt = date_to or all_months[-1]
        # Нормализуем 'YYYY-MM-DD' → 'YYYY-MM'
        df = df[:7]
        dt = dt[:7]
        selected = [m for m in all_months if df <= m <= dt]
        if not selected:
            return {
                "error": f"Нет данных за период {df}..{dt}",
                "available_range": [all_months[0], all_months[-1]],
            }

        target_articles = articles
        if target_articles:
            # Нормализуем имена через канонизацию
            target_articles = [_canonical_article(a) for a in target_articles]
            known = set(totals.keys())
            missing = [a for a in target_articles if a not in known]
            target_articles = [a for a in target_articles if a in known]
            if not target_articles:
                # Подсказываем что есть
                sample = sorted(totals.keys())[:30]
                return {
                    "error": f"Статьи не найдены: {missing}",
                    "available_articles_sample": sample,
                    "total_articles": len(totals),
                }
        else:
            target_articles = sorted(totals.keys())

        result_rows = []
        for a in target_articles:
            monthly = totals.get(a) or {}
            values = [round(monthly.get(m, 0.0) or 0.0) for m in selected]
            total = round(sum(values))
            result_rows.append({
                "article": a,
                "total": total,
                "by_month": dict(zip(selected, values)),
            })

        # Сортируем по сумме за период (по убыванию) для удобства анализа
        result_rows.sort(key=lambda x: abs(x["total"]), reverse=True)

        return {
            "period": {"from": selected[0], "to": selected[-1], "months": len(selected)},
            "articles_count": len(result_rows),
            "articles": result_rows[:60],  # cap to keep payload tractable
            "note": (
                "Все суммы из колонки 'Итого' помесячных листов. "
                "Отсортированы по абсолютной сумме за период."
            ),
        }

    async def month_detail(self, year_month: str) -> Dict[str, Any]:
        """Полный ОПиУ за один месяц с разбивкой Москва / Казань / Итого.

        year_month: 'YYYY-MM' (например '2026-04').
        """
        ym = (year_month or "").strip()[:7]
        if not re.match(r"^\d{4}-\d{2}$", ym):
            return {"error": "year_month должен быть в формате YYYY-MM"}

        totals, percents, by_month = await self._load()
        all_months = self._available_months(totals)
        if ym not in all_months:
            return {
                "error": f"Нет данных за {ym}",
                "available_range": [all_months[0], all_months[-1]] if all_months else [],
            }

        detail = by_month.get(ym) or {}
        # Если детализации нет (старые листы без филиалов) — отдадим только Итого
        articles_block = []
        for article, totals_map in detail.items():
            articles_block.append({
                "article": article,
                "Москва": round(totals_map.get("Москва", 0.0) or 0.0),
                "Казань": round(totals_map.get("Казань", 0.0) or 0.0),
                "Итого": round(totals_map.get("Итого", 0.0) or 0.0),
            })
        if not articles_block:
            # Fallback: только Итого из общего totals
            for article, monthly in totals.items():
                v = monthly.get(ym)
                if v is not None:
                    articles_block.append({
                        "article": article,
                        "Итого": round(v),
                    })

        # KPI для месяца
        def get(article: str) -> float:
            return self._get(totals, article, ym)

        revenue = get("Выручка")
        ebitda = get("Операционная прибыль (EBITDA)")
        net = get("Чистая прибыль Итого") or get("Чистая прибыль")
        margin = get("Маржинальный доход")
        gross = get("Валовая прибыль")
        mkt = sum(get(a) for a in _MARKETING_ARTICLES)

        # % из листа если есть
        def pct(article: str) -> Optional[float]:
            return (percents.get(article) or {}).get(ym)

        kpi = {
            "revenue": round(revenue),
            "margin": round(margin),
            "margin_pct": pct("Рентабельность по маржинальному") or pct("Рентабельность по маржинальному доходу") or self._safe_pct(margin, revenue),
            "gross_profit": round(gross),
            "gross_pct": pct("Рентабельность по ВП, %") or pct("Рентабельность по ВП") or self._safe_pct(gross, revenue),
            "ebitda": round(ebitda),
            "ebitda_pct": pct("Рентабельность по операционной прибыли (EBITDA)") or pct("Рентабельность по операционной") or self._safe_pct(ebitda, revenue),
            "net_profit": round(net),
            "net_pct": pct("Рентабельность по чистой прибыли") or self._safe_pct(net, revenue),
            "marketing": round(mkt),
            "drr_pct": self._safe_pct(mkt, revenue),
        }

        return {
            "month": ym,
            "kpi": kpi,
            "articles": articles_block,
            "note": (
                "Москва/Казань заполнены для 2025+. Для 2023-2024 — только Итого "
                "(в листах одна колонка 'Growzone')."
            ),
        }


pnl_client = PnLSheetClient()
