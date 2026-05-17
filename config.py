from pydantic_settings import BaseSettings
from pydantic import Field


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

    # Database
    database_path: str = "./data/bot.sqlite"

    # Bot settings
    log_level: str = "INFO"
    session_ttl_minutes: int = 30
    max_session_messages: int = 20

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
