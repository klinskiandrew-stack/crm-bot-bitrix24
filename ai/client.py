from anthropic import AsyncAnthropic
from typing import Optional, List, Dict, Any
import time
import structlog
from config import settings

logger = structlog.get_logger()


class KieAIClient:
    def __init__(self):
        self.client = AsyncAnthropic(
            api_key=settings.kie_api_key,
            base_url=settings.kie_base_url,
        )
        self.default_model = "claude-sonnet-4-6"

    async def send_message(
        self,
        messages: List[Dict[str, Any]],
        system: str = None,
        model: str = None,
        tools: List[Dict[str, Any]] = None,
        max_tokens: int = 1024,
        temperature: float = 0.7
    ) -> Dict[str, Any]:
        """Send a message to Claude via Kie.ai."""
        start_time = time.time()
        model = model or self.default_model

        try:
            kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
                "temperature": temperature,
            }

            if system:
                kwargs["system"] = system

            if tools:
                kwargs["tools"] = tools

            response = await self.client.messages.create(**kwargs)

            duration_ms = int((time.time() - start_time) * 1000)

            # Build usage info with proper null checking
            usage_info = {}
            if response.usage:
                usage_info = {
                    "input_tokens": getattr(response.usage, "input_tokens", 0),
                    "output_tokens": getattr(response.usage, "output_tokens", 0),
                    "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
                    "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
                }

            result = {
                "content": response.content,
                "stop_reason": response.stop_reason,
                "usage": usage_info,
                "model": response.model,
                "duration_ms": duration_ms,
            }

            # Extract credits_consumed from response if available
            if hasattr(response, "credits_consumed"):
                result["credits_consumed"] = response.credits_consumed

            logger.info(
                "Claude API call successful",
                model=model,
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                duration_ms=duration_ms
            )

            return result

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(
                "Claude API call failed",
                error=str(e),
                model=model,
                duration_ms=duration_ms
            )
            raise

    async def set_default_model(self, model: str):
        """Set default model for future requests."""
        self.default_model = model
        logger.info("Default model updated", model=model)
