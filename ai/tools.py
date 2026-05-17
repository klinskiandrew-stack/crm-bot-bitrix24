"""Tool definitions for Claude function calling."""


def get_tools_definitions():
    """Get list of tool definitions for Claude."""
    return [
        {
            "name": "get_deals",
            "description": (
                "Получить список сделок из CRM Bitrix24. ИСПОЛЬЗУЙ для любых вопросов "
                "про сделки, продажи, оборот, конверсию, источники трафика. "
                "Возвращает по каждой сделке: ID, название, стадию, сумму, ответственного, "
                "источник (SOURCE_ID, SOURCE_DESCRIPTION) и ПОЛНЫЕ UTM-метки "
                "(UTM_SOURCE, UTM_MEDIUM, UTM_CAMPAIGN, UTM_CONTENT, UTM_TERM). "
                "По умолчанию 20 сделок. ВАЖНО: одного вызова обычно достаточно — "
                "НЕ дёргай get_deal_full для каждой сделки чтобы узнать UTM, они уже здесь."
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
            "description": (
                "Получить список лидов из CRM. Используй для вопросов про лиды, "
                "новых клиентов, входящие обращения, источники трафика. "
                "Возвращает по каждому лиду: ID, название, статус, имя, источник "
                "(SOURCE_ID, SOURCE_DESCRIPTION) и ПОЛНЫЕ UTM-метки. "
                "Для отчётов «лиды по источникам / UTM» ОДНОГО вызова достаточно — "
                "НЕ дёргай get_lead_full для каждого лида."
            ),
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
                "Получить ПОЛНУЮ карточку ОДНОГО лида со всеми кастомными полями "
                "(metrika_client_id, marquiz_ym_uid, ответы квиза в COMMENTS, "
                "заметки менеджера). ВАЖНО: для массовых отчётов НЕ вызывай этот "
                "инструмент циклом — UTM-метки и источник уже есть в get_leads. "
                "Используй ТОЛЬКО для 1-2 конкретных лидов."
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
                "Получить ПОЛНУЮ карточку ОДНОЙ сделки со всеми кастомными полями "
                "и комментариями менеджеров. ВАЖНО: для массовых отчётов "
                "(сделки по UTM-источникам, по менеджерам и т.п.) НЕ вызывай этот "
                "инструмент циклом — UTM-метки уже есть в get_deals. Используй "
                "ТОЛЬКО для 1-2 конкретных сделок когда нужны детали."
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
            "name": "metrika_traffic_summary",
            "description": (
                "Получить общие показатели трафика сайта из Яндекс.Метрики за период: "
                "визиты, уникальные посетители, просмотры, % отказов, глубина просмотра, "
                "среднее время на сайте. ИСПОЛЬЗУЙ для вопросов 'сколько визитов', "
                "'посещаемость сайта', 'отказы', 'трафик за неделю/месяц'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "Начало периода YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "Конец периода YYYY-MM-DD (включительно)"}
                },
                "required": ["date_from", "date_to"]
            }
        },
        {
            "name": "metrika_traffic_by_source",
            "description": (
                "Разбивка трафика сайта из Яндекс.Метрики по источникам: UTM-метки или "
                "канал (search/ad/direct/social/referral). Для каждого источника — визиты, "
                "уников и % отказов. ИСПОЛЬЗУЙ для 'топ источников трафика', "
                "'откуда приходят посетители', 'какая UTM-кампания лучшая'. "
                "Для связки CRM-лиды vs трафик — сначала вызови этот, потом get_leads с "
                "тем же периодом и сопоставь по UTM_SOURCE/UTM_CAMPAIGN."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "Начало периода YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "Конец периода YYYY-MM-DD"},
                    "breakdown": {
                        "type": "string",
                        "description": (
                            "По чему разбивать: utm_source (по умолчанию), utm_medium, "
                            "utm_campaign, utm_content, utm_term, channel (общий канал)."
                        )
                    },
                    "limit": {"type": "integer", "description": "Сколько строк показать (по умолчанию 20, макс 50)"}
                },
                "required": ["date_from", "date_to"]
            }
        },
        {
            "name": "lus_financials",
            "description": (
                "Финансовая аналитика из Google Sheet ЛУС Growzone (лист 'Сделки'): "
                "выручка план/факт, маржинальная прибыль, расходы, дебиторка, "
                "рентабельность. Это ВНУТРЕННЯЯ учётная таблица, она может расходиться "
                "с Bitrix24. ИСПОЛЬЗУЙ для вопросов про деньги: 'сколько заработали', "
                "'какая маржа', 'какие сделки в дебиторке', 'рентабельность по направлению'. "
                "Поддерживает группировку и фильтр по 'Завершен' (выручка признана)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "Начало периода YYYY-MM-DD по полю 'Дата продажи'"},
                    "date_to": {"type": "string", "description": "Конец периода YYYY-MM-DD"},
                    "group_by": {
                        "type": "string",
                        "description": (
                            "Группировка: 'Источник клиента' (Авито/Яндекс Директ/...), "
                            "'Направление' (GrowZoneMSK/...), 'Услуга' (Автополив/Замер/...), "
                            "'Партнер', 'Месяц', 'Статус'. Без значения — общая сводка."
                        )
                    },
                    "only_completed": {
                        "type": "boolean",
                        "description": "Только сделки со Статусом 'Завершен' (где выручка признана)."
                    }
                },
                "required": []
            }
        },
        {
            "name": "lus_get_deal",
            "description": (
                "Полная карточка ОДНОЙ сделки из таблицы ЛУС по её ID (порядковый "
                "номер 1-496 в таблице, НЕ deal_id Bitrix). Содержит все 33 поля: "
                "контрагент, направление, услугу, даты, план/факт по расходам, "
                "маржу, рентабельность, дебиторку."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "ID строки в таблице ЛУС (1-496)"}
                },
                "required": ["id"]
            }
        },
        {
            "name": "lus_search",
            "description": (
                "Поиск в таблице ЛУС по контрагенту, номеру договора или городу "
                "(substring match). Возвращает совпавшие строки."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Часть имени контрагента / номера договора / города"},
                    "limit": {"type": "integer", "description": "Сколько строк показать (по умолчанию 10, макс 30)"}
                },
                "required": ["query"]
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
