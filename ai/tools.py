"""Tool definitions for Claude function calling."""


def get_tools_definitions():
    """Get list of tool definitions for Claude."""
    return [
        {
            "name": "get_deals",
            "description": "Получить список сделок с фильтрацией. Возвращает сделки, назначенные текущему пользователю.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "filter_by_stage": {
                        "type": "string",
                        "description": "Фильтр по стадии сделки (например, 'PREPARATION', 'NEGOTIATION'). Опциональный."
                    },
                    "filter_by_date_from": {
                        "type": "string",
                        "description": "Фильтр по дате создания (от). Формат: YYYY-MM-DD. Опциональный."
                    },
                    "filter_by_date_to": {
                        "type": "string",
                        "description": "Фильтр по дате создания (до). Формат: YYYY-MM-DD. Опциональный."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное количество результатов (по умолчанию 50, максимум 500)"
                    }
                },
                "required": []
            }
        },
        {
            "name": "get_deal_details",
            "description": "Получить подробную информацию по конкретной сделке (контакт, компания, сумма, стадия, история).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "deal_id": {
                        "type": "integer",
                        "description": "ID сделки в Bitrix24"
                    }
                },
                "required": ["deal_id"]
            }
        },
        {
            "name": "get_leads",
            "description": "Получить список лидов с фильтрацией. Возвращает лиды, назначенные текущему пользователю.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "filter_by_status": {
                        "type": "string",
                        "description": "Фильтр по статусу лида. Опциональный."
                    },
                    "filter_by_date_from": {
                        "type": "string",
                        "description": "Фильтр по дате создания (от). Формат: YYYY-MM-DD. Опциональный."
                    },
                    "filter_by_date_to": {
                        "type": "string",
                        "description": "Фильтр по дате создания (до). Формат: YYYY-MM-DD. Опциональный."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное количество результатов (по умолчанию 50)"
                    }
                },
                "required": []
            }
        },
        {
            "name": "search_contacts_or_companies",
            "description": "Поиск контакта или компании по названию, телефону или email.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Поисковый запрос (имя, телефон, email)"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное количество результатов (по умолчанию 20)"
                    }
                },
                "required": ["query"]
            }
        },
        {
            "name": "get_pipeline_summary",
            "description": "Получить агрегированную сводку по воронке продаж (количество и сумма сделок по каждой стадии за период).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_from": {
                        "type": "string",
                        "description": "Дата начала периода. Формат: YYYY-MM-DD. Опциональный."
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Дата конца периода. Формат: YYYY-MM-DD. Опциональный."
                    }
                },
                "required": []
            }
        },
        {
            "name": "get_user_activity_summary",
            "description": "Получить сводку активности пользователя за период (новые сделки, закрытые, средний цикл продаж).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_from": {
                        "type": "string",
                        "description": "Дата начала периода. Формат: YYYY-MM-DD. Опциональный."
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Дата конца периода. Формат: YYYY-MM-DD. Опциональный."
                    }
                },
                "required": []
            }
        },
        {
            "name": "get_recent_activities",
            "description": "Получить последние активности (задачи, звонки, встречи) текущего пользователя.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное количество результатов (по умолчанию 20)"
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "Количество дней в прошлое (по умолчанию 7)"
                    }
                },
                "required": []
            }
        }
    ]
