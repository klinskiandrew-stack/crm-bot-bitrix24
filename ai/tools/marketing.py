"""Marketing tool definitions (Yandex Metrika, Avito)."""

MARKETING_TOOLS = [
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
        "name": "avito_balance",
        "description": (
            "Текущий баланс Avito-аккаунта (рубли + бонусы). "
            "Используй для вопросов 'сколько на счету Avito', 'хватит ли денег'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "avito_items",
        "description": (
            "Список активных объявлений на Avito с пагинацией (загружает все страницы). "
            "Возвращает id, title, цена, адрес, категория, URL. "
            "У Growzone ~195 активных объявлений."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_items": {
                    "type": "integer",
                    "description": "Hard limit на количество (по умолчанию 500). Для аккаунта Growzone достаточно 200."
                }
            },
            "required": []
        }
    },
    {
        "name": "avito_stats",
        "description": (
            "Главный tool для статистики Avito. Считает по ВСЕМ активным объявлениям (пагинация + батчинг). "
            "Возвращает: total_views, total_uniq_views, total_contacts, total_favorites за период.\n\n"
            "ВАЖНО про метрики:\n"
            "• uniqContacts = ОБРАЩЕНИЯ от клиентов (клики 'Позвонить' + 'Написать'). "
            "Это то, что в кабинете Avito показано как 'Контакты'. Когда пользователь спрашивает "
            "про звонки / контакты / обращения с Avito — используй это число.\n"
            "• uniqFavorites = добавления в избранное.\n"
            "• uniqViews = уникальные просмотры объявлений.\n"
            "• Реальные звонки с записью (calltracking) у Growzone отключены — это платная услуга."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "avito_spend",
        "description": (
            "Расходы и пополнения на Avito за период: разбивка по типам услуг "
            "(CPA, тарифы, продвижение), общий списанный объём, сторно, депозиты. "
            "ИСПОЛЬЗУЙ для 'сколько мы потратили на Avito', 'на что тратим бюджет', 'ROI Avito'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "avito_calls",
        "description": (
            "Записи реальных звонков из коллтрекинга Avito. "
            "У Growzone коллтрекинг ОТКЛЮЧЁН (платная услуга Avito Pro), endpoint вернёт пустой результат. "
            "Не используй этот tool для метрики обращений — используй avito_stats (uniqContacts)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Максимум звонков (по умолчанию 50, макс 100)"}
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "avito_weak_ads",
        "description": (
            "Анализ объявлений: классифицирует на 3 группы. ИСПОЛЬЗУЙ когда "
            "пользователь спрашивает «что улучшить на Avito», «какие объявления "
            "плохо работают», «что чинить», «какие масштабировать».\n\n"
            "Возвращает 3 группы (с примерами по 10 шт):\n"
            "• **high_views_no_contacts** — ГЛАВНАЯ боль: люди видят, но не "
            "  обращаются (≥20 views, 0 contacts). Конкретные id и title — "
            "  именно их надо переделать: фото / цена / заголовок / УТП.\n"
            "• **stars** — высокая конверсия (>10%) или ≥3 contacts. Кандидаты "
            "  на масштабирование (продвижение/копирование в другие города).\n"
            "• **no_views_in_period** — нет показов за период. Это НЕ "
            "  «мёртвые» — алгоритм Avito ротирует выдачу. Можно попробовать "
            "  продвижение или обновление.\n\n"
            "Дешевле и полезнее чем дёргать avito_stats + потом разбираться руками."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "min_views_for_dead_zone": {
                    "type": "integer",
                    "description": "Сколько views минимум чтобы считать 'просмотры есть но обращений 0' проблемой (по умолчанию 20)."
                }
            },
            "required": ["date_from", "date_to"]
        }
    },
    {
        "name": "avito_funnel",
        "description": (
            "Главный tool для ROI/окупаемости Avito. Связывает 4 источника данных:\n"
            "1) Расход на Avito (avito_spend) — сколько потратили\n"
            "2) Обращения в Avito (avito_stats: uniqContacts) — клики «Позвонить/Написать»\n"
            "3) Лиды в Bitrix24 с источником Avito (по SOURCE_ID + phone_pool)\n"
            "4) Сделки в Bitrix24 с источником Avito (won + сумма)\n\n"
            "Возвращает воронку и расчёты: CPL (cost-per-lead), CAC (cost-of-acquisition), "
            "ROI, конверсии contact→lead и lead→deal.\n\n"
            "ИСПОЛЬЗУЙ ВСЕГДА когда пользователь спрашивает: «окупается ли Avito», "
            "«сколько лидов с Avito», «выручка с Avito», «ROI Avito», «эффективность Avito». "
            "Намного полезнее чем avito_spend + avito_stats по отдельности."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"}
            },
            "required": ["date_from", "date_to"]
        }
    },
]
