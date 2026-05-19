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
