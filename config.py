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

    # Google Sheets (optional). Service account JSON path. SA must have read access to the sheet.
    google_sa_path: str = "/opt/crm-bot/secrets/google_sa.json"

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

    # Circuit breaker — Level 3 (global daily)
    daily_global_credits_limit: float = 5000.0  # ~$25 at 1cr=$0.005

    class Config:
        env_file = ".env"
        case_sensitive = False

    @property
    def b24_portal_url(self) -> str:
        """Portal base URL (e.g. 'https://growzone.bitrix24.ru') derived from webhook."""
        parsed = urlparse(self.b24_webhook_url)
        return f"{parsed.scheme}://{parsed.netloc}"


settings = Settings()
