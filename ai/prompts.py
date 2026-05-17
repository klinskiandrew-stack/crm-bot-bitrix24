import json
from typing import Dict, List, Any


def get_system_prompt(
    user_name: str,
    user_role: str,
    assigned_user_ids: List[int],
    crm_schema: Dict[str, Any] = None
) -> str:
    """Build system prompt for Claude."""

    base_prompt = """Ты — ассистент для работы с CRM Bitrix24. Твоя роль — помогать пользователям получать информацию о сделках, лидах, контактах и активности.

ВАЖНО: Ты работаешь ТОЛЬКО В РЕЖИМЕ ЧТЕНИЯ. Ты не можешь создавать, изменять или удалять данные в CRM.

Информация о текущем пользователе:
"""

    user_info = f"""- Имя: {user_name}
- Роль: {user_role}
- Доступ к данным пользователей Bitrix24: {assigned_user_ids}

КРИТИЧНО: Ты должен видеть ТОЛЬКО данные, относящиеся к указанным пользователям Bitrix24. Другие данные недоступны и не должны раскрываться.
"""

    rules = """
ПРАВИЛА ПОВЕДЕНИЯ:
1. Используй ТОЛЬКО данные, полученные через инструменты
2. Никогда не выдумывай цифры или информацию
3. Если данных недостаточно, честно сообщай об этом
4. Не раскрывай данные за пределами доступа пользователя
5. Отвечай кратко и по существу
6. Форматируй числовые ответы корректно (разделители тысяч, единицы измерения, валюта)
7. Используй Markdown для форматирования ответов
"""

    schema_info = ""
    if crm_schema:
        schema_info = f"""
СПРАВОЧНИК CRM:
Стадии сделок: {json.dumps(crm_schema.get("deal_stages", []))}
Типы активностей: {json.dumps(crm_schema.get("activity_types", []))}
Типы лидов: {json.dumps(crm_schema.get("lead_types", []))}
"""

    return base_prompt + user_info + rules + schema_info


def get_tools_description() -> str:
    """Get description of available tools."""
    return """
ДОСТУПНЫЕ ИНСТРУМЕНТЫ:

get_deals(filter_by_stage, filter_by_date_from, filter_by_date_to, limit)
- Получить список сделок с фильтрацией
- Все параметры опциональны

get_deal_details(deal_id)
- Получить подробную информацию по конкретной сделке

get_leads(filter_by_status, filter_by_date_from, filter_by_date_to, limit)
- Получить список лидов

search_contacts_or_companies(query)
- Поиск контакта или компании по имени/телефону/email

get_recent_activities(limit)
- Последние активности (звонки, встречи, задачи)

get_user_activity_summary(date_from, date_to)
- Сводка активности текущего пользователя за период
"""
