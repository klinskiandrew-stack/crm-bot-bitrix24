"""Tool definitions for Claude function calling."""


def get_tools_definitions():
    """Get list of tool definitions for Claude."""
    return [
        {
            "name": "get_deals",
            "description": (
                "Получить список сделок из CRM Bitrix24. ИСПОЛЬЗУЙ для любых вопросов "
                "про сделки, продажи, оборот, конверсию. По умолчанию возвращает 20 сделок "
                "со всеми статусами. ВАЖНО: один вызов обычно достаточен — "
                "не вызывай повторно с другими параметрами без необходимости."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "filter_by_stage": {
                        "type": "string",
                        "description": (
                            "Фильтр по конкретной стадии. Формат строго 'C{категория}:{код}', "
                            "например 'C0:NEW' (новая в воронке по умолчанию), 'C2:WON' (выиграна), "
                            "'C2:LOSE' (проиграна). Если не знаешь точный код — НЕ указывай, "
                            "лучше отфильтруй по семантике в полученных данных."
                        )
                    },
                    "filter_by_date_from": {
                        "type": "string",
                        "description": (
                            "Сделки, созданные начиная с этой даты (включительно). "
                            "Строгий формат: YYYY-MM-DD (например 2026-05-01). "
                            "Вычисляй относительно сегодняшней даты из системного промпта."
                        )
                    },
                    "filter_by_date_to": {
                        "type": "string",
                        "description": "Сделки, созданные до этой даты (включительно). Формат: YYYY-MM-DD."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимум сделок в ответе. По умолчанию 20, максимум 100. Больше — только если явно нужно."
                    }
                },
                "required": []
            }
        },
        {
            "name": "get_deal_details",
            "description": "Получить подробную информацию по конкретной сделке по её ID (все поля, контакты, компания, сумма, стадия).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "deal_id": {
                        "type": "integer",
                        "description": "Числовой ID сделки в Bitrix24"
                    }
                },
                "required": ["deal_id"]
            }
        },
        {
            "name": "get_leads",
            "description": "Получить список лидов из CRM. Используй для вопросов про лиды, новых клиентов, входящие обращения.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "filter_by_status": {
                        "type": "string",
                        "description": "Фильтр по статусу лида (например 'NEW', 'IN_PROCESS', 'CONVERTED', 'JUNK'). Если не уверен — не указывай."
                    },
                    "filter_by_date_from": {
                        "type": "string",
                        "description": "Лиды от этой даты создания. Формат: YYYY-MM-DD."
                    },
                    "filter_by_date_to": {
                        "type": "string",
                        "description": "Лиды до этой даты создания. Формат: YYYY-MM-DD."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимум 100 (по умолчанию 20)."
                    }
                },
                "required": []
            }
        },
        {
            "name": "search_contacts_or_companies",
            "description": "Поиск контактов по части имени (substring). Возвращает совпадения с телефонами и email.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Часть имени/фамилии для поиска (substring match). Например 'Иван' найдёт всех Иванов."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимум совпадений (по умолчанию 20)."
                    }
                },
                "required": ["query"]
            }
        },
        {
            "name": "get_pipeline_summary",
            "description": (
                "Получить агрегированную сводку по воронке продаж: количество и сумма сделок "
                "по каждой стадии за период. ИСПОЛЬЗУЙ для вопросов 'покажи воронку', "
                "'сколько продаж и на сколько', 'какой оборот за месяц'. Возвращает агрегаты, не сырые сделки."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_from": {
                        "type": "string",
                        "description": "Начало периода. Формат: YYYY-MM-DD. Если не указано — берутся все доступные."
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Конец периода. Формат: YYYY-MM-DD."
                    }
                },
                "required": []
            }
        },
        {
            "name": "get_user_activity_summary",
            "description": "Сводка активности пользователя за период: новые сделки, количество активностей по типам.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_from": {
                        "type": "string",
                        "description": "Начало периода. Формат: YYYY-MM-DD."
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Конец периода. Формат: YYYY-MM-DD."
                    }
                },
                "required": []
            }
        },
        {
            "name": "count_deals_passed_stage",
            "description": (
                "Подсчитать сделки, ПРОШЕДШИЕ через определённую стадию воронки за период. "
                "Учитывает ИСТОРИЮ (даже если сделка уже на следующей стадии или провалена). "
                "ИСПОЛЬЗУЙ для замеров, продаж за период, конверсий. "
                "Возвращает: общее число событий, число уникальных сделок, "
                "СПИСОК deals с TITLE и card_url — для вывода ссылок не нужно делать "
                "дополнительных вызовов get_deal_details."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "stage_id": {
                        "type": "string",
                        "description": (
                            "Код стадии (без префикса воронки). Известные коды основной воронки 'Автополивы Сделки':\n"
                            "- 'UC_BFLJ2N' — Замер выполнен\n"
                            "- 'PREPARATION' — Договор заключён (внесён аванс) — для подсчёта ПРОДАЖ\n"
                            "- 'WON' — Сделка завершена\n"
                            "- 'LOSE' — Сделка провалена"
                        )
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Начало периода YYYY-MM-DD (включительно)."
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Конец периода YYYY-MM-DD (включительно)."
                    },
                    "category_id": {
                        "type": "integer",
                        "description": "ID воронки. По умолчанию 0 (основная — 'Автополивы Сделки'). Другие: 13=Автополив Монтаж, 3=Сервисное обслуживание."
                    }
                },
                "required": ["stage_id", "date_from", "date_to"]
            }
        },
        {
            "name": "get_lead_full",
            "description": (
                "Получить ПОЛНУЮ карточку лида со ВСЕМИ полями — стандартными "
                "(имя, телефон, email, UTM-метки, источник, COMMENTS с ответами квиза) "
                "и кастомными (metrika_client_id, marquiz_ym_uid, Город, Площадь участка, "
                "и т.д.). Возвращает только заполненные поля + комментарии менеджеров "
                "из таймлайна карточки. ИСПОЛЬЗУЙ когда нужны детали по конкретному лиду: "
                "UTM-метки, ответы из квиза, заметки менеджера."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "lead_id": {
                        "type": "integer",
                        "description": "Числовой ID лида в Bitrix24"
                    }
                },
                "required": ["lead_id"]
            }
        },
        {
            "name": "get_deal_full",
            "description": (
                "Получить ПОЛНУЮ карточку сделки со всеми полями включая кастомные "
                "(UTM-метки, metrika_client_id, площадь, причины, и т.д.) и комментарии "
                "менеджеров из таймлайна. Возвращает только заполненные поля. "
                "ИСПОЛЬЗУЙ для детального анализа конкретной сделки — например причины "
                "проигрыша, UTM-источник, переписку из таймлайна."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "deal_id": {
                        "type": "integer",
                        "description": "Числовой ID сделки в Bitrix24"
                    }
                },
                "required": ["deal_id"]
            }
        },
        {
            "name": "get_card_comments",
            "description": (
                "Получить только комментарии менеджеров из таймлайна карточки лида или "
                "сделки (без остальных полей). ИСПОЛЬЗУЙ когда нужны именно заметки/"
                "обсуждения по карточке — например для анализа причин отказа."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "description": "Тип: 'lead' или 'deal'"
                    },
                    "entity_id": {
                        "type": "integer",
                        "description": "ID лида или сделки"
                    }
                },
                "required": ["entity_type", "entity_id"]
            }
        },
        {
            "name": "get_recent_activities",
            "description": "Последние активности (звонки, встречи, задачи). Используй для 'что было сделано на этой неделе', 'последние звонки'.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Максимум результатов (по умолчанию 20)."
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "За сколько дней назад смотреть (по умолчанию 7)."
                    }
                },
                "required": []
            }
        }
    ]
