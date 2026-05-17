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
