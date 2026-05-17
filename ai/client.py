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
            default_headers={
                "Authorization": f"Bearer {settings.kie_api_key}",
            },
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

            # Only add tools if list is not empty
            if tools and len(tools) > 0:
                kwargs["tools"] = tools

            logger.debug(
                "Sending request to Kie.ai",
                model=model,
                system_prompt_len=len(system) if system else 0,
                messages_count=len(messages),
                tools_count=len(tools) if tools else 0,
                has_system=bool(system),
                first_message_role=messages[0].get("role") if messages else None
            )

            response = await self.client.messages.create(**kwargs)

            logger.debug(
                "Received response from Kie.ai",
                response_model=response.model,
                response_type=type(response).__name__,
                has_content=bool(response.content),
                content_count=len(response.content) if hasattr(response, 'content') else 0,
                stop_reason=response.stop_reason if hasattr(response, 'stop_reason') else 'N/A'
            )

            duration_ms = int((time.time() - start_time) * 1000)

            logger.debug(
                "Raw response from Kie.ai",
                response_type=type(response).__name__,
                has_content=hasattr(response, 'content'),
                has_usage=hasattr(response, 'usage'),
                content=str(response.content) if hasattr(response, 'content') else 'N/A',
                usage=str(response.usage) if hasattr(response, 'usage') else 'N/A',
                stop_reason=response.stop_reason if hasattr(response, 'stop_reason') else 'N/A'
            )

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
                "content": response.content if hasattr(response, 'content') else [],
                "stop_reason": response.stop_reason if hasattr(response, 'stop_reason') else None,
                "usage": usage_info,
                "model": response.model if hasattr(response, 'model') else model,
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
