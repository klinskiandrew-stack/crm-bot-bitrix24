"""DeepSeek client with Anthropic-compatible interface.

Translates between our Anthropic-style request/response (tool_use blocks,
tool_result, system as string) and DeepSeek's OpenAI-compatible format
(tool_calls, role:tool, system as message). Orchestrator stays agnostic.
"""

import aiohttp
import asyncio
import json
import random
import time
import structlog
from typing import Any, Dict, List, Optional

from config import settings

logger = structlog.get_logger()

# Per-model pricing in USD per 1M tokens.
# Source: https://api-docs.deepseek.com/quick_start/pricing
# Cache HIT is ~50x cheaper than miss — billing was off by ~50x before
# we split these out.
PRICING = {
    "deepseek-v4-flash": {"in_miss": 0.14,  "in_hit": 0.0028,   "out": 0.28},
    "deepseek-v4-pro":   {"in_miss": 0.435, "in_hit": 0.003625, "out": 0.87},
    # fallback for unknown / legacy aliases
    "_default":          {"in_miss": 0.14,  "in_hit": 0.0028,   "out": 0.28},
}

# Map DeepSeek's finish_reason → orchestrator's stop_reason vocabulary.
# Orchestrator only branches on 'end_turn' / 'tool_use' so the rest are
# treated as terminal — but we log a warning on truncation/error states.
STOP_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "end_turn",            # truncated by max_tokens
    "content_filter": "end_turn",
    "insufficient_system_resource": "end_turn",
}


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

    Also filters orphaned role:tool messages — if a session was truncated
    mid-cycle, a tool_result may reference a tool_call_id that no longer
    appears in the preceding assistant turn. DeepSeek rejects that with 400.
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
                msg["content"] = None
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)

        elif role == "user":
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

    # Filter orphaned tool messages — DeepSeek 400 if tool_call_id has no
    # preceding assistant tool_call. Collect valid ids from assistant turns,
    # then drop any role:tool referencing an unknown id.
    valid_ids = {
        tc["id"]
        for msg in out
        if msg.get("role") == "assistant"
        for tc in (msg.get("tool_calls") or [])
        if tc.get("id")
    }
    filtered: List[Dict[str, Any]] = []
    for msg in out:
        if msg.get("role") == "tool" and msg.get("tool_call_id") not in valid_ids:
            logger.debug(
                "Dropping orphaned tool message",
                tool_call_id=msg.get("tool_call_id"),
            )
            continue
        filtered.append(msg)
    return filtered


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
        max_tokens: int = 2048,
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
            # Disable thinking mode. With thinking enabled, the model emits
            # reasoning_content alongside tool_calls — and DeepSeek then
            # requires us to echo that reasoning_content back on the next
            # turn or it 400s. Plus it costs extra output tokens we don't
            # need for CRM lookups.
            "thinking": {"type": "disabled"},
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

        # Retry on 429 (rate limit) and 5xx (transient). DeepSeek uses
        # dynamic concurrency limits — 429 is a normal back-pressure signal,
        # not a quota exhaustion.
        raw = None
        resp_status = None
        last_err = None
        for attempt in range(3):
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
                    resp_status = resp.status
                    text = await resp.text()
                    try:
                        raw = json.loads(text)
                    except json.JSONDecodeError:
                        logger.error("DeepSeek non-JSON response", status=resp_status, body=text[:300])
                        raise RuntimeError(f"DeepSeek returned non-JSON (status={resp_status}): {text[:200]}")

                if resp_status == 429 or 500 <= resp_status < 600:
                    last_err = f"DeepSeek API {resp_status}: {str(raw)[:200]}"
                    if attempt < 2:
                        delay = (2 ** attempt) + random.random() * 0.5
                        logger.warning("DeepSeek transient error, retrying", attempt=attempt, status=resp_status, delay=delay)
                        await asyncio.sleep(delay)
                        continue

                if resp_status != 200 or "error" in raw:
                    msg = (raw.get("error") or {}).get("message") if isinstance(raw.get("error"), dict) else raw
                    logger.error("DeepSeek API error", status=resp_status, response=str(raw)[:300])
                    raise RuntimeError(f"DeepSeek API {resp_status}: {msg}")

                break  # success
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = str(e)
                if attempt < 2:
                    delay = (2 ** attempt) + random.random() * 0.5
                    logger.warning("DeepSeek network error, retrying", attempt=attempt, error=str(e), delay=delay)
                    await asyncio.sleep(delay)
                    continue
                raise

        if raw is None:
            raise RuntimeError(f"DeepSeek failed after retries: {last_err}")

        try:
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

            stop_reason = STOP_REASON_MAP.get(finish_reason, "end_turn")
            if finish_reason in ("length", "insufficient_system_resource", "content_filter"):
                logger.warning(
                    "DeepSeek truncated/filtered response",
                    finish_reason=finish_reason,
                    model=model,
                )

            usage = raw.get("usage", {})
            cache_hit = usage.get("prompt_cache_hit_tokens") or 0
            cache_miss = (
                usage.get("prompt_cache_miss_tokens")
                if usage.get("prompt_cache_miss_tokens") is not None
                else max(0, usage.get("prompt_tokens", 0) - cache_hit)
            )
            out_tok = usage.get("completion_tokens", 0)

            usage_info = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": out_tok,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cache_hit,
                "cache_miss_input_tokens": cache_miss,
            }

            # Per-model pricing (cache hit ~50x cheaper than miss).
            p = PRICING.get(model) or PRICING["_default"]
            usd = (
                cache_hit * p["in_hit"]
                + cache_miss * p["in_miss"]
                + out_tok * p["out"]
            ) / 1_000_000
            # Express in "credits" (Kie convention: 1 cr ≈ $0.005) for unified accounting.
            credits_consumed = usd / 0.005

            logger.info(
                "DeepSeek API call successful",
                model=model,
                stop_reason=stop_reason,
                finish_reason=finish_reason,
                input_tokens=usage_info["input_tokens"],
                output_tokens=out_tok,
                cache_hit=cache_hit,
                cache_miss=cache_miss,
                usd=round(usd, 6),
                credits=round(credits_consumed, 4),
                duration_ms=duration_ms,
            )

            return {
                "content": content_blocks,
                "stop_reason": stop_reason,
                "usage": usage_info,
                "model": raw.get("model", model),
                "duration_ms": duration_ms,
                "credits_consumed": credits_consumed,
            }

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error("DeepSeek response parse failed", error=str(e), duration_ms=duration_ms)
            raise

    async def set_default_model(self, model: str):
        # No-op: orchestrator may feed Anthropic-style names; we always
        # use settings.deepseek_model in send_message.
        return
