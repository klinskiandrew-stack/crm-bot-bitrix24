"""Бизнес-стадии воронки Growzone — единый источник правды.

Главная воронка (CATEGORY_ID=0) Growzone устроена так:
  Квалификация → Замер → КП → Согласование → ДОГОВОР+АВАНС → Монтаж → Финал

КОММЕРЧЕСКАЯ ПОБЕДА — на стадии `PREPARATION` («Договор заключён, внесён
аванс»). Дальше идут производственные этапы (монтаж), которые формально
ещё «открытые сделки» по флагу CLOSED=N, но по сути уже продажи.

Стадия `WON` («Сделка завершена — остаток получен») — это только финал
по деньгам и работам, может растягиваться на 1-3 месяца после
PREPARATION. Считать выручку и конверсию ТОЛЬКО по WON — занижает
картину; РОПу нужны цифры по PREPARATION+.

Эти константы используются:
  • growth_intel/funnel.py — bucket «won» расширен до 6 стадий
  • growth_intel/digest.py — фильтр выигранных сделок
  • ai/tool_handlers.py::sales_forecast — won_so_far + actual revenue
  • ai/prompts.py — system prompt напоминает LLM об этом правиле
"""

# Стадии «уже продано» — с момента заключения договора до финала.
# Сделка переходит сюда → деньги в кармане (хотя бы частично).
DONE_DEAL_STAGES = (
    "PREPARATION",      # Договор заключён (внесён аванс) — БИЗНЕС-ПОБЕДА
    "UC_5EL81I",        # Оборудование закуплено
    "UC_84WK2U",        # Монтаж
    "UC_BSBCZY",        # Монтаж завершён
    "UC_68KU96",        # Монтаж завершён (Филиалы)
    "WON",              # Сделка завершена (остаток получен)
)

# Стадии «провалена / отказ»
LOST_DEAL_STAGES = (
    "LOSE",
    "APOLOGY",
)

# Маркеры для семантической классификации stage_id в _bucket
# (используется когда stage_id не из главной воронки — например C2:..,
# C9:.. для других CATEGORY_ID).
DONE_STAGE_MARKERS = (
    "PREPARATION", "WON", "MONTAJ", "МОНТАЖ", "ЗАКЛЮЧЕН", "АВАНС",
    "ОБОРУД",
)


def is_done(stage_id: str) -> bool:
    """Это бизнес-победа (есть аванс / договор / монтаж / финал)?"""
    if not stage_id:
        return False
    s = str(stage_id).upper()
    # Точное совпадение по главной воронке
    if s in DONE_DEAL_STAGES:
        return True
    # Кастомные воронки: ":PREPARATION", ":WON" и т.п.
    if any(s.endswith(":" + m) or s == m for m in DONE_DEAL_STAGES):
        return True
    # Семантика по словам в названии (для UC_XXX-стадий с переводом)
    if any(m in s for m in DONE_STAGE_MARKERS):
        return True
    return False


def is_lost(stage_id: str) -> bool:
    """Это провал/отказ?"""
    if not stage_id:
        return False
    s = str(stage_id).upper()
    if s in LOST_DEAL_STAGES:
        return True
    if any(s.endswith(":" + m) or s == m for m in LOST_DEAL_STAGES):
        return True
    if "LOST" in s or "JUNK" in s:
        return True
    return False
