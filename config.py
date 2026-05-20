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

    # Kie.ai
    kie_api_key: str
    kie_base_url: str = "https://api.kie.ai/claude"
    kie_proxy_url: str = ""

    # DeepSeek (optional, used when llm_provider=deepseek)
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

    # Which provider to use for LLM calls: "kie" or "deepseek"
    llm_provider: str = "kie"

    # Database
    database_path: str = "./data/bot.sqlite"

    # Bot settings
    log_level: str = "INFO"
    session_ttl_minutes: int = 30
    max_session_messages: int = 20

    # Circuit breakers — Level 1 (per-request)
    max_iterations: int = 5
    max_request_input_tokens: int = 150_000  # cumulative across iterations
    max_request_credits: float = 30.0        # cumulative across iterations
    max_tool_calls_per_request: int = 8      # total tool invocations per request

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
