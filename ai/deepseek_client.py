"""DeepSeek client with Anthropic-compatible interface.

Translates between our Anthropic-style request/response (tool_use blocks,
tool_result, system as string) and DeepSeek's OpenAI-compatible format
(tool_calls, role:tool, system as message). Orchestrator stays agnostic.
"""

import aiohttp
import json
import time
import structlog
from typing import Any, Dict, List, Optional

from config import settings

logger = structlog.get_logger()


class _TextBlock:
    """Anthropic-style text content block (orchestrator uses .text)."""
    __slots__ = ("text", "type")

    def __init__(self, text: str):
        self.text = text
        self.type = "text"


class _ToolUseBlock:
    """Anthropic-style tool_use block (orchestrator uses .id/.name/.input/.type)."""
    __slots__ = ("id", "name", "input", "type")

    def __init__(self, tool_id: str, name: str, tool_input: Dict[str, Any]):
        self.id = tool_id
        self.name = name
        self.input = tool_input
        self.type = "tool_use"


def _tools_anthropic_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert our Anthropic-style tool defs to OpenAI function-tool defs."""
    out = []
    for t in tools or []:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            }
        })
    return out


def _messages_anthropic_to_openai(
    system: Optional[str],
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert our Anthropic-style messages history to OpenAI chat format.

    Anthropic structure we receive:
    - {role: 'user', content: 'text'}  OR  {role: 'user', content: [tool_result blocks]}
    - {role: 'assistant', content: 'text'}  OR  {role: 'assistant', content: [text/tool_use blocks]}
    """
    out: List[Dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for m in messages:
        role = m["role"]
        content = m["content"]

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            out.append({"role": role, "content": str(content)})
            continue

        if role == "assistant":
            # Assistant message: may have text blocks and tool_use blocks.
            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            for block in content:
                btype = _block_attr(block, "type")
                if btype == "text":
                    text_parts.append(_block_attr(block, "text", "") or "")
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": _block_attr(block, "id"),
                        "type": "function",
                        "function": {
                            "name": _block_attr(block, "name"),
                            "arguments": json.dumps(
                                _block_attr(block, "input", {}) or {},
                                ensure_ascii=False,
                            ),
                        },
                    })
            msg: Dict[str, Any] = {"role": "assistant"}
            if text_parts:
                msg["content"] = "\n".join(text_parts)
            else:
                # OpenAI requires content to be present (can be null) when tool_calls used
                msg["content"] = None
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)

        elif role == "user":
            # User message: may carry tool_result blocks (from our orchestrator).
            tool_results: List[Dict[str, Any]] = []
            text_parts: List[str] = []
            for block in content:
                btype = _block_attr(block, "type")
                if btype == "tool_result":
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": _block_attr(block, "tool_use_id"),
                        "content": _block_attr(block, "content", "") or "",
                    })
                elif btype == "text":
                    text_parts.append(_block_attr(block, "text", "") or "")
            if text_parts:
                out.append({"role": "user", "content": "\n".join(text_parts)})
            out.extend(tool_results)

    return out


def _block_attr(block: Any, name: str, default=None):
    """Read attr from either dict or object."""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


class DeepSeekClient:
    """OpenAI-compatible client that mimics our KieAIClient surface."""

    def __init__(self):
        self.api_key = settings.deepseek_api_key
        self.base_url = settings.deepseek_base_url.rstrip("/")
        # Always sourced from settings — ignore orchestrator's set_default_model
        # which feeds Anthropic-style names ('claude-sonnet-4-6') that DeepSeek
        # rejects with 400.
        self.default_model = settings.deepseek_model
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def send_message(
        self,
        messages: List[Dict[str, Any]],
        system: str = None,
        model: str = None,
        tools: List[Dict[str, Any]] = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> Dict[str, Any]:
        """Send a message to DeepSeek, return Anthropic-style response dict."""
        start_time = time.time()
        await self._ensure_session()
        # Always force the configured DeepSeek model. The orchestrator's
        # router emits Anthropic names ('claude-sonnet-4-6') which DeepSeek
        # rejects with 400 — those names are meaningless here.
        model = settings.deepseek_model

        body = {
            "model": model,
            "messages": _messages_anthropic_to_openai(system, messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = _tools_anthropic_to_openai(tools)

        logger.debug(
            "Sending request to DeepSeek",
            model=model,
            messages_count=len(body["messages"]),
            tools_count=len(body.get("tools") or []),
            has_system=bool(system),
        )

        try:
            async with self._session.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as resp:
                # content_type=None: bypass aiohttp's strict mime check.
                # CloudFront in front of DeepSeek occasionally returns
                # application/octet-stream even on a valid JSON body.
                text = await resp.text()
                try:
                    raw = json.loads(text)
                except json.JSONDecodeError:
                    logger.error("DeepSeek non-JSON response", status=resp.status, body=text[:300])
                    raise RuntimeError(f"DeepSeek returned non-JSON (status={resp.status}): {text[:200]}")

                if resp.status != 200 or "error" in raw:
                    msg = (raw.get("error") or {}).get("message") if isinstance(raw.get("error"), dict) else raw
                    logger.error("DeepSeek API error", status=resp.status, response=str(raw)[:300])
                    raise RuntimeError(f"DeepSeek API {resp.status}: {msg}")

            duration_ms = int((time.time() - start_time) * 1000)
            choice = raw["choices"][0]
            msg = choice["message"]
            finish_reason = choice.get("finish_reason", "stop")

            # Build Anthropic-style content blocks
            content_blocks: List[Any] = []
            if msg.get("content"):
                content_blocks.append(_TextBlock(msg["content"]))
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                content_blocks.append(_ToolUseBlock(
                    tool_id=tc.get("id", ""),
                    name=fn.get("name", ""),
                    tool_input=args,
                ))

            stop_reason = "tool_use" if finish_reason == "tool_calls" else "end_turn"

            usage = raw.get("usage", {})
            usage_info = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": (usage.get("prompt_cache_hit_tokens") or 0),
            }

            logger.info(
                "DeepSeek API call successful",
                model=model,
                stop_reason=stop_reason,
                input_tokens=usage_info["input_tokens"],
                output_tokens=usage_info["output_tokens"],
                cache_read=usage_info["cache_read_input_tokens"],
                duration_ms=duration_ms,
            )

            return {
                "content": content_blocks,
                "stop_reason": stop_reason,
                "usage": usage_info,
                "model": raw.get("model", model),
                "duration_ms": duration_ms,
                # DeepSeek charges in USD by tokens — convert to "credits" for unified accounting.
                # Approx: $0.14/1M input, $0.28/1M output, 1cr = $0.005 (Kie convention).
                "credits_consumed": (
                    usage_info["input_tokens"] * 0.14 / 1_000_000
                    + usage_info["output_tokens"] * 0.28 / 1_000_000
                ) / 0.005,
            }

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error("DeepSeek API call failed", error=str(e), duration_ms=duration_ms)
            raise

    async def set_default_model(self, model: str):
        # No-op: orchestrator may feed Anthropic-style names; we always
        # use settings.deepseek_model in send_message.
        return
