-- Пользователи бота с маппингом на Bitrix24
CREATE TABLE IF NOT EXISTS users (
    telegram_id        INTEGER PRIMARY KEY,
    role               TEXT NOT NULL CHECK (role IN ('admin', 'partner', 'viewer')),
    b24_user_ids       TEXT,                  -- JSON-массив ID ответственных в Б24
    display_name       TEXT,
    is_active          INTEGER DEFAULT 1,
    allow_private      INTEGER DEFAULT 1,     -- 0 = только групповые чаты, без личных сообщений
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Idempotent migration: добавить колонку если её ещё нет (для существующих БД)
-- SQLite поддерживает ALTER TABLE ADD COLUMN; ошибку "duplicate column" игнорируем
-- через PRAGMA на стороне приложения.

-- Глобальные настройки бота (key-value)
-- Ключи: current_model, routing_mode (auto|forced), default_model
CREATE TABLE IF NOT EXISTS settings (
    key                TEXT PRIMARY KEY,
    value              TEXT NOT NULL,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Состояние диалогов (для поддержания контекста в группах)
CREATE TABLE IF NOT EXISTS sessions (
    telegram_id        INTEGER NOT NULL,
    chat_id            INTEGER NOT NULL,
    messages_json      TEXT NOT NULL,         -- последние N сообщений в формате Anthropic messages
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (telegram_id, chat_id)
);

-- Журнал всех запросов для аудита и аналитики
CREATE TABLE IF NOT EXISTS audit_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id        INTEGER,
    chat_id            INTEGER,
    chat_type          TEXT,                  -- private | group | supergroup
    question           TEXT,
    model_used         TEXT,
    tools_called       TEXT,                  -- JSON-массив имён инструментов
    answer             TEXT,
    input_tokens       INTEGER,
    cached_input_tokens INTEGER,
    output_tokens      INTEGER,
    credits_consumed   REAL,                  -- из ответа Kie
    duration_ms        INTEGER,
    error              TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Опциональный кэш данных Bitrix24 (для часто запрашиваемых статичных данных)
CREATE TABLE IF NOT EXISTS b24_cache (
    cache_key          TEXT PRIMARY KEY,
    payload            TEXT NOT NULL,
    expires_at         TIMESTAMP NOT NULL
);

-- Индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_audit_telegram_id_created ON audit_log(telegram_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);
CREATE INDEX IF NOT EXISTS idx_b24_cache_expires ON b24_cache(expires_at);

-- =========================================================================
-- Meetings module: participant tracking, voting polls, scheduled meetings.
-- =========================================================================

-- Список реальных людей, которых видели в чате (для пикера участников).
-- is_bot=0 строго: ботов не сохраняем вовсе.
CREATE TABLE IF NOT EXISTS chat_members (
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    full_name    TEXT,
    username     TEXT,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_members_chat ON chat_members(chat_id);

-- Опрос для согласования слота созвона.
-- status: 'picking_participants' -> 'voting' -> 'manual_pick' -> 'finalized' / 'cancelled'
CREATE TABLE IF NOT EXISTS meeting_polls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    initiator_id    INTEGER NOT NULL,
    meeting_date    TEXT NOT NULL,                -- YYYY-MM-DD (MSK)
    duration_min    INTEGER NOT NULL DEFAULT 60,
    topic           TEXT,
    status          TEXT NOT NULL DEFAULT 'picking_participants',
    message_id      INTEGER,                       -- id сообщения с опросом в чате
    chosen_hour     INTEGER,                       -- 10..17 после финализации
    zoom_meeting_id INTEGER,                       -- ссылка на scheduled_meetings.id
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deadline_at     TIMESTAMP,                     -- авто-закрытие (UTC)
    finalized_at    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_meeting_polls_status ON meeting_polls(status, deadline_at);
CREATE INDEX IF NOT EXISTS idx_meeting_polls_chat ON meeting_polls(chat_id);

-- Участники опроса (выбранные инициатором).
CREATE TABLE IF NOT EXISTS meeting_poll_participants (
    poll_id   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    full_name TEXT,
    PRIMARY KEY (poll_id, user_id),
    FOREIGN KEY (poll_id) REFERENCES meeting_polls(id) ON DELETE CASCADE
);

-- Голоса по слотам: 'yes' или 'no'. Отсутствие записи = 'не отметился'.
CREATE TABLE IF NOT EXISTS meeting_poll_votes (
    poll_id   INTEGER NOT NULL,
    slot_hour INTEGER NOT NULL,    -- 10..17 (час начала)
    user_id   INTEGER NOT NULL,
    vote      TEXT NOT NULL CHECK (vote IN ('yes', 'no')),
    PRIMARY KEY (poll_id, slot_hour, user_id),
    FOREIGN KEY (poll_id) REFERENCES meeting_polls(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_meeting_poll_votes_poll ON meeting_poll_votes(poll_id);

-- Запланированные созвоны (для напоминаний).
CREATE TABLE IF NOT EXISTS scheduled_meetings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id            INTEGER NOT NULL,
    initiator_id       INTEGER,
    topic              TEXT,
    start_at           TIMESTAMP NOT NULL,         -- UTC
    duration_min       INTEGER NOT NULL DEFAULT 60,
    zoom_meeting_id    TEXT,
    zoom_join_url      TEXT NOT NULL,
    zoom_start_url     TEXT,
    participants_json  TEXT,                        -- JSON [{user_id, full_name}, ...]
    reminder_sent_at   TIMESTAMP,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scheduled_meetings_reminder ON scheduled_meetings(start_at, reminder_sent_at);

-- Лиды из чата «sphere ИТМ» (бот Amely постит «Онлайн отчётность»).
-- Собираются Telethon-listener'ом модуля lead_reports.
-- status: parsed → transcribed → done | error.
CREATE TABLE IF NOT EXISTS lead_reports (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id           INTEGER NOT NULL UNIQUE,   -- дедуп по id сообщения чата
    chat_id              INTEGER,
    call_datetime        TEXT,                      -- '2026-05-20 15:36:00' (МСК)
    inn                  TEXT,
    company              TEXT,
    phone                TEXT,
    position             TEXT,
    fio                  TEXT,
    lpr_phone            TEXT,
    email                TEXT,
    city                 TEXT,
    comment              TEXT,
    recording_url        TEXT,
    recording_local_path TEXT,                      -- Этап 2: скачанный MP3
    transcript           TEXT,                      -- Этап 2: STT
    ai_summary           TEXT,                      -- Этап 3: LLM-анализ
    ai_client_need       TEXT,
    ai_manager_score     INTEGER,
    ai_manager_comment   TEXT,
    ai_lead_temp         TEXT,                      -- горячий/тёплый/холодный
    status               TEXT NOT NULL DEFAULT 'parsed',
    error                TEXT,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at         TIMESTAMP,
    exported_at          TIMESTAMP,                 -- когда строка ушла в Google Sheet
    -- Этап 4: сквозная связка с CRM Bitrix24 (по телефону клиента)
    b24_lead_id          INTEGER,                   -- найденный лид
    b24_deal_id          INTEGER,                   -- найденная сделка (если лид сконвертирован)
    crm_outcome          TEXT,                      -- Квал / Неквал / В работе / Не найдено
    crm_deal_stage       TEXT,                      -- текущая стадия сделки (название)
    crm_deal_result      TEXT,                      -- Успешна / Провалена / В работе
    crm_deal_amount      REAL,                      -- сумма сделки (OPPORTUNITY)
    crm_had_measurement  TEXT,                      -- Был / Не было
    crm_reason           TEXT,                      -- причина отказа (UF_CRM_1723465843)
    crm_manager_comment  TEXT,                      -- поле «Комментарий» карточки лида
    crm_card_url         TEXT,                      -- ссылка на карточку в Bitrix
    crm_synced_at        TIMESTAMP,                 -- когда последний раз обновляли из CRM
    notify_message_id    INTEGER                    -- id прогресс-сообщения бота в чате sphere ИТМ
);
CREATE INDEX IF NOT EXISTS idx_lead_reports_status ON lead_reports(status);
CREATE INDEX IF NOT EXISTS idx_lead_reports_call_dt ON lead_reports(call_datetime);

-- =========================================================================
-- sales_comms module: единая база коммуникаций по сделкам.
--
-- Накопляет на стороне бота все «прикосновения» менеджера к клиенту по
-- конкретной сделке: комментарии в таймлайне, активити (звонки/задачи/
-- письма), расшифровки звонков и сообщения из Open Lines (WhatsApp,
-- Telegram, чат на сайте). Питается фоновым sales_comms_sync cron'ом,
-- читается инструментом deals_status_digest когда РОП спрашивает «что
-- происходит по моим сделкам».
--
-- Цель: один запрос пользователя ≠ десяток обращений к Bitrix. Бот
-- читает уже готовую локальную БД, DeepSeek группирует факты.
-- =========================================================================
CREATE TABLE IF NOT EXISTS deal_communications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id             INTEGER NOT NULL,
    source_type         TEXT NOT NULL,         -- 'comment' | 'call' | 'task' | 'email' | 'openline'
    source_id           TEXT NOT NULL,         -- ID объекта в Bitrix (activity, comment, im message)
    direction           TEXT,                  -- 'in' | 'out' | NULL (для комментариев — NULL)
    author_id           INTEGER,               -- Bitrix user id; NULL если клиент / автомат
    author_name         TEXT,                  -- кэш имени для дайджеста, чтобы не дёргать users_map
    occurred_at         TIMESTAMP NOT NULL,    -- когда событие случилось (МСК)
    subject             TEXT,                  -- тема: для email/task — заголовок, для OL — имя линии
    text                TEXT,                  -- тело: коммент/расшифровка звонка/тело email/сообщение OL
    audio_url           TEXT,                  -- если звонок — ссылка на запись (для transcribe worker)
    duration_sec        INTEGER,               -- длительность звонка
    transcription_status TEXT,                 -- 'pending' | 'done' | 'failed' | 'n/a' (для не-звонков)
    transcription_error TEXT,
    raw_meta            TEXT,                  -- JSON с прочими полями activity/сообщения на случай если потом пригодится
    synced_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_type, source_id)
);
CREATE INDEX IF NOT EXISTS idx_dc_deal_time      ON deal_communications(deal_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_dc_pending        ON deal_communications(transcription_status) WHERE transcription_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_dc_source_type    ON deal_communications(source_type, occurred_at DESC);

-- Состояние синка по сделке: чтобы инкрементально подтягивать новое и
-- знать когда последний раз ходили. Без этого каждый sync будет
-- перетягивать всю историю.
CREATE TABLE IF NOT EXISTS deal_sync_state (
    deal_id              INTEGER PRIMARY KEY,
    last_synced_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_comment_id      INTEGER,              -- макс. ID комментария что мы уже знаем
    last_activity_id     INTEGER,              -- макс. ID активности
    last_openline_msg_id INTEGER,              -- макс. ID сообщения OL
    deal_stage           TEXT,                 -- текущая стадия (для быстрого фильтра в digest)
    deal_status_semantic TEXT,                 -- 'P' | 'S' | 'F' (process/success/fail) для skip закрытых
    sync_error           TEXT                  -- последняя ошибка sync для отладки
);
CREATE INDEX IF NOT EXISTS idx_dss_synced ON deal_sync_state(last_synced_at);

-- =========================================================================
-- growth_intel module: семантические сигналы из коммуникаций по сделкам.
--
-- Анализатор пробегает по deal_communications за период, скармливает
-- DeepSeek'у переписку и расшифровки звонков, и извлекает структурированные
-- "триггеры" — сигналы где менеджер может (или должен) что-то сделать,
-- чтобы не потерять клиента.
--
-- Категории (см. growth_intel/triggers.py:TRIGGER_CATEGORIES):
--   client_ready_to_pay        — клиент готов оплатить
--   client_promised_deadline   — клиент назвал дату обещания
--   manager_promised_action    — менеджер обещал клиенту срок
--   client_question_unanswered — клиент задал вопрос без ответа
--   objection_not_handled      — клиент возразил, не отработано
--   decision_signal            — клиент дал решение к покупке
--
-- satisfied — флаг что менеджер реально среагировал на сигнал (по умолчанию
-- false; меняем когда видим в коммуникациях ответное действие).
-- =========================================================================
CREATE TABLE IF NOT EXISTS growth_signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id        INTEGER NOT NULL,
    category       TEXT NOT NULL,         -- см. TRIGGER_CATEGORIES
    detected_at    TIMESTAMP NOT NULL,    -- когда событие в коммуникациях случилось
    deadline       TIMESTAMP,             -- срок до которого ждём действия (если был)
    evidence       TEXT NOT NULL,         -- цитата/пересказ из коммуникации
    value_at_risk  REAL,                  -- сумма сделки на момент детекта (₽)
    manager_id     INTEGER,               -- кто ответственный
    satisfied      INTEGER DEFAULT 0,     -- 1 если менеджер уже отработал
    satisfied_at   TIMESTAMP,
    satisfied_note TEXT,
    severity       TEXT DEFAULT 'medium', -- 'low' | 'medium' | 'high' (для приоритизации)
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(deal_id, category, detected_at)
);
CREATE INDEX IF NOT EXISTS idx_gs_unsatisfied ON growth_signals(satisfied, severity, deadline);
CREATE INDEX IF NOT EXISTS idx_gs_deal        ON growth_signals(deal_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_gs_manager     ON growth_signals(manager_id, satisfied, detected_at DESC);

-- =========================================================================
-- sales_comms Lv3: расширенный синк по сделке.
-- Хранит контакты клиента, прикреплённые файлы, счета и историю стадий,
-- чтобы deals_status_digest / growth_opportunities могли давать полную
-- картину «что у нас по сделке» без живых дёрганий Bitrix24 на каждый
-- запрос РОПа.
-- =========================================================================

-- Контакты сделки (Bitrix: crm.contact.get по CONTACT_IDS из сделки).
-- Один контакт может быть привязан к нескольким сделкам — храним связь
-- как many-to-many через PK (deal_id, contact_id). PHONE/EMAIL храним
-- через ; если значений несколько.
CREATE TABLE IF NOT EXISTS deal_contacts (
    deal_id      INTEGER NOT NULL,
    contact_id   INTEGER NOT NULL,
    name         TEXT,            -- ФИО полностью
    phone        TEXT,            -- основной телефон или несколько через "; "
    email        TEXT,
    position     TEXT,            -- должность
    company      TEXT,            -- название компании (если есть COMPANY_ID)
    is_primary   INTEGER DEFAULT 0,
    synced_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (deal_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_deal_contacts_deal ON deal_contacts(deal_id);

-- Файлы, прикреплённые к таймлайну сделки (КП, договор, скан паспорта).
-- Источник: FILES в crm.activity, + crm.timeline.bindings.list по сделке.
-- size_bytes/mime_type помогают LLM понять «pdf-договор 200KB» vs
-- «фото 4MB». uploaded_by → ID менеджера для понимания «кто загрузил».
CREATE TABLE IF NOT EXISTS deal_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id      INTEGER NOT NULL,
    file_id      TEXT NOT NULL,     -- ID файла в Bitrix Disk
    name         TEXT NOT NULL,
    size_bytes   INTEGER,
    mime_type    TEXT,
    uploaded_at  TIMESTAMP,
    uploaded_by  INTEGER,           -- Bitrix user_id, если известно
    activity_id  TEXT,              -- crm.activity.ID если файл прикреплён к активности
    download_url TEXT,              -- DOWNLOAD_URL от disk.file.get (короткоживущий)
    synced_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(deal_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_deal_files_deal ON deal_files(deal_id, uploaded_at DESC);

-- Счета (crm.invoice.list или /invoicessmart/ — у Bitrix два разных API,
-- проверим какой работает у Growzone).
-- status_id у smart-invoice: 'P' = paid, 'N' = draft, 'D' = overdue.
CREATE TABLE IF NOT EXISTS deal_invoices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id        INTEGER NOT NULL,
    invoice_id     INTEGER NOT NULL,
    invoice_number TEXT,
    amount_rub     REAL,
    currency       TEXT DEFAULT 'RUB',
    status_id      TEXT,         -- внутренний код Bitrix
    status_name    TEXT,         -- человекочитаемое (paid / draft / overdue)
    created_at_b24 TIMESTAMP,
    paid_at        TIMESTAMP,
    due_at         TIMESTAMP,
    synced_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(invoice_id)
);
CREATE INDEX IF NOT EXISTS idx_deal_invoices_deal ON deal_invoices(deal_id);

-- История стадий сделки (crm.stagehistory.list, entityTypeId=2).
-- Для каждой стадии в которой сделка побывала — когда вошла, когда
-- вышла (или NULL если ещё там), сколько дней висела. Это даёт ответ
-- на «застряла в КП 18 дней» без живого вычисления.
CREATE TABLE IF NOT EXISTS deal_stage_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id       INTEGER NOT NULL,
    stage_id      TEXT NOT NULL,            -- UC_BFLJ2N / PREPARATION / WON ...
    stage_name    TEXT,                     -- расшифровка («Замер выполнен»)
    entered_at    TIMESTAMP NOT NULL,
    exited_at     TIMESTAMP,                -- NULL = текущая стадия
    duration_days INTEGER,                  -- (exited_at - entered_at) или (now - entered_at)
    synced_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(deal_id, stage_id, entered_at)
);
CREATE INDEX IF NOT EXISTS idx_deal_stage_history_deal ON deal_stage_history(deal_id, entered_at);
