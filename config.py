from pydantic_settings import BaseSettings
from pydantic import Field
from urllib.parse import urlparse


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str
    admin_telegram_id: int
    telegram_proxy_url: str = ""

    # Bitrix24
    b24_webhook_url: str

    # Kie.ai — НЕ используется (бот работает исключительно через DeepSeek).
    # Поля оставлены опциональными на случай возврата к Kie; пустой ключ
    # больше не мешает боту запуститься.
    kie_api_key: str = ""
    kie_base_url: str = "https://api.kie.ai/claude"
    kie_proxy_url: str = ""

    # DeepSeek — основной (и единственный) LLM-провайдер бота.
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # Yandex Metrika (optional). Token from oauth.yandex.ru with metrika:read scope.
    metrika_oauth_token: str = ""
    metrika_counter_id: str = ""
    metrika_base_url: str = "https://api-metrika.yandex.net"

    # Avito API (optional). client_id/secret from developers.avito.ru → Мои приложения.
    # Uses client_credentials flow (Приложение персональной авторизации).
    # AVITO_USER_ID = "Номер профиля" in кабинете разработчика.
    avito_client_id: str = ""
    avito_client_secret: str = ""
    avito_user_id: str = ""

    # Google Sheets (optional). Service account JSON path. SA must have read access to the sheet.
    google_sa_path: str = "/opt/crm-bot/secrets/google_sa.json"

    # Zoom (Server-to-Server OAuth). marketplace.zoom.us → Build App → S2S OAuth.
    # Required scopes: meeting:write:meeting, user:read:user.
    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: str = ""

    # Meetings module. Toggle to disable entirely without removing code.
    meetings_enabled: bool = True
    meetings_timezone: str = "Europe/Moscow"
    meetings_work_start_hour: int = 10
    meetings_work_end_hour: int = 18
    meetings_default_duration_min: int = 60
    meetings_poll_timeout_min: int = 60
    meetings_reminder_min_before: int = 30

    # Scheduled reports (sent to a Telegram chat at fixed cron times).
    # Override via .env. Empty REPORTS_CHAT_ID disables the scheduler.
    reports_enabled: bool = True
    reports_chat_id: int = 0   # e.g. -1003939434094
    reports_timezone: str = "Europe/Moscow"
    reports_daily_hour: int = 9
    reports_daily_minute: int = 0

    # Lead reports module (collects lead reports from the sphere ИТМ chat
    # via Telethon, transcribes calls, stores them). Toggle off to disable.
    lead_reports_enabled: bool = False
    lead_reports_chat_id: int = 0          # basic group id, e.g. -4151474068
    # Telethon (User API). api_id/api_hash from my.telegram.org — set in .env.
    # The session file holds the actual authorisation; never commit it.
    telethon_api_id: int = 0
    telethon_api_hash: str = ""
    telethon_session_path: str = "/opt/crm-bot/secrets/telethon_lead_reports"
    # Call transcription (faster-whisper, local). Model size trades RAM
    # for quality: tiny ~75MB, base ~150MB, small ~500MB. int8 = CPU-friendly.
    whisper_model: str = "small"
    whisper_compute_type: str = "int8"
    lead_recordings_dir: str = "/opt/crm-bot/data/lead_recordings"
    # Google Sheet for transcribed leads. Empty = export disabled.
    # Share the sheet with the service account as Editor.
    lead_reports_sheet_id: str = ""

    # Voice commands — transcribe a Telegram voice message / video-note
    # with the local Whisper and answer it like a typed question. Reuses
    # the lead-reports STT (faster-whisper). Toggle off to disable.
    voice_commands_enabled: bool = True

    # Sales intelligence — proactively detects stuck / forgotten deals and
    # leads, plus a weekly "sales opportunities" digest into the РОП chat.
    sales_intel_enabled: bool = True
    sales_digest_chat_id: int = 0           # РОП chat, e.g. -5122320352
    sales_digest_weekday: str = "mon"       # APScheduler day_of_week
    sales_digest_hour: int = 9
    sales_digest_minute: int = 30
    # Ежедневный отчёт «работа менеджеров за вчера» → чат РОПа в 09:00 МСК.
    manager_daily_enabled: bool = True
    manager_daily_chat_id: int = 0          # РОП chat, e.g. -5122320352
    manager_daily_hour: int = 9
    manager_daily_minute: int = 0

    # sales_comms — единая база коммуникаций по сделкам (комментарии,
    # звонки, активность, Open Lines). Питает инструмент
    # deals_status_digest. Cron-sync включается флагом ниже.
    sales_comms_enabled: bool = True
    sales_comms_sync_minute: int = 17       # запуск каждый час на :17 (не лезет в другие cron'ы)
    # Whisper-расшифровка звонков — самая RAM-тяжёлая часть (1.2GB на
    # модель). Можно отключить если сервер делит память с другими
    # сервисами и упирается в OOM. Сводки/триггеры будут работать на
    # уже расшифрованных звонках (см. transcription_status='done').
    sales_comms_transcribe_enabled: bool = True

    # growth_intel — отчёт «где теряем деньги» с триггерами из переписки.
    # Тяжёлый (DeepSeek по 60-100 сделкам, ~5-10 мин). Сейчас встроен в
    # ежедневный manager_daily-отчёт как второе сообщение — см.
    # reports/manager_daily.py::send_manager_daily.
    growth_intel_enabled: bool = True
    # ⚠️ Старый еженедельный отдельный cron (понедельник 09:15) отключён
    # по умолчанию — задаётся через growth_intel_weekly_enabled=true,
    # если когда-нибудь захотим параллельно с ежедневным.
    growth_intel_weekly_enabled: bool = False
    growth_intel_weekday: str = "mon"
    growth_intel_hour: int = 9
    growth_intel_minute: int = 15

    stuck_deal_days: int = 14               # open deal not moved this long
    stuck_deal_max_days: int = 90           # older than this = dead, skip
    cold_lead_days: int = 2                 # active lead untouched this long
    measurement_followup_days: int = 7      # замер done, deal not progressed
    new_lead_react_hours: int = 4           # new lead with no first touch

    # LLM-провайдер. Бот работает исключительно через DeepSeek — это
    # значение по умолчанию. "kie" оставлен только как явный аварийный
    # переключатель через .env, в норме не используется.
    llm_provider: str = "deepseek"

    # Database
    database_path: str = "./data/bot.sqlite"

    # Bot settings
    log_level: str = "INFO"
    session_ttl_minutes: int = 30
    max_session_messages: int = 20

    # Circuit breakers — Level 1 (per-request)
    max_iterations: int = 5
    max_request_input_tokens: int = 250_000  # cumulative across iterations
    # 2026-05-28: поднято со 150K до 250K. На прогнозных вопросах (пр.
    # «на какую сумму до конца мая может продать отдел?») 5 итераций
    # get_deals накапливают 200-220K из-за повторной отправки tool_results.
    # Cache hit на промпте/tools покрывает ~28K фактически бесплатно
    # (0.0028 vs 0.14 / 1M токенов, ~50× дешевле), так что финансово
    # лимит безболезненно растёт. Реальная защита — max_request_credits.
    max_request_credits: float = 30.0        # cumulative across iterations
    max_tool_calls_per_request: int = 12     # total tool invocations per request
    # Поднято с 8 → 12 после анализа отладки 2026-05: аналитические вопросы
    # ("прогноз продаж на май", "статистика Авито с замерами") честно
    # требуют 9-11 вызовов (несколько get_deals по стадиям + сводки).
    # На уровне токенов их ограничивает max_request_input_tokens.

    # Circuit breaker — Level 3 (global daily)
    daily_global_credits_limit: float = 5000.0  # ~$25 at 1cr=$0.005

    # Dashboard (HTTP-сервис для внешних специалистов по трафику).
    # Поля читаются через os.getenv в main.py/dashboard/app.py, но должны
    # быть объявлены здесь, иначе pydantic-settings отклонит .env.
    dashboard_enabled: str = "1"
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8001
    dashboard_refresh_minutes: int = 5
    dashboard_token: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"

    @property
    def b24_portal_url(self) -> str:
        """Portal base URL (e.g. 'https://growzone.bitrix24.ru') derived from webhook."""
        parsed = urlparse(self.b24_webhook_url)
        return f"{parsed.scheme}://{parsed.netloc}"


settings = Settings()
