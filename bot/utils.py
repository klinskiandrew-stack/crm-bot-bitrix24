from typing import Optional
import structlog

logger = structlog.get_logger()


def get_proxy_config(proxy_url: str = None) -> Optional[str]:
    """Get proxy configuration for aiogram."""
    if not proxy_url:
        return None

    # Validate proxy URL format
    if proxy_url.startswith(("http://", "https://", "socks5://", "socks5h://")):
        logger.info("Using proxy", proxy_url="***")
        return proxy_url
    else:
        logger.warning("Invalid proxy URL format", proxy_url="***")
        return None


def extract_mention_and_text(text: str, entities: list) -> tuple:
    """Extract mention and remaining text from message."""
    mention = None
    for entity in entities:
        if entity.type == "mention":
            mention = text[entity.offset:entity.offset + entity.length]
            break

    remaining_text = text
    if mention:
        remaining_text = text.replace(mention, "").strip()

    return mention, remaining_text
