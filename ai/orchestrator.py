from typing import List, Dict, Any, Optional, Callable, Awaitable
import json
import time
import structlog
from ai.client import KieAIClient
from ai.deepseek_client import DeepSeekClient
from ai.router import router
from ai.tools import get_tools_definitions
from ai.tool_handlers import handlers
from config import settings

logger = structlog.get_logger()


def _make_client():
    """LLM client per settings.llm_provider — 'deepseek' by default.

    The bot runs exclusively on DeepSeek; KieAIClient stays only as an
    explicit opt-in fallback (llm_provider=kie in .env) and is otherwise
    never instantiated.
    """
    provider = (settings.llm_provider or "deepseek").lower()
    if provider == "kie":
        logger.info("LLM provider initialized", provider="kie")
        return KieAIClient()
    if not settings.deepseek_api_key:
        raise RuntimeError("llm_provider=deepseek but DEEPSEEK_API_KEY is empty")
    logger.info("LLM provider initialized", provider="deepseek", model=settings.deepseek_model)
    return DeepSeekClient()


class Orchestrator:
    """Manage conversation with Claude including function calling."""

    def __init__(self):
        self.client = _make_client()
        self.max_iterations = settings.max_iterations
        self.max_input_tokens = settings.max_request_input_tokens
        self.max_credits = settings.max_request_credits
        self.max_tool_calls = settings.max_tool_calls_per_request
        self.tool_definitions = get_tools_definitions()
        # When cumulative input passes this fraction of the hard limit,
        # trim old tool_results down to a placeholder. Keeps the most
        # recent K turns intact so the model still has its working data.
        self.trim_threshold_ratio = 0.4  # 40% of max_input_tokens — start early
        self.keep_recent_tool_results = 1  # how many recent results stay full

    def _trim_old_tool_results(self, messages: List[Dict[str, Any]]) -> int:
        """Replace JSON of old tool_results with short placeholders.

        Each new iteration in the tool-use loop drags ALL prior tool_results
        back into the API request — context grows linearly per round. After
        a few rounds the same get_leads JSON has been re-sent 5+ times.

        Strategy: find user-messages whose content is a list of tool_result
        blocks. Keep the last `keep_recent_tool_results` of them intact;
        rewrite older ones to a one-line placeholder noting what was there.
        Returns count of trimmed results."""
        tool_result_positions = []
        for idx, msg in enumerate(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            if all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                tool_result_positions.append(idx)

        # How many to trim from the start
        n_to_trim = max(0, len(tool_result_positions) - self.keep_recent_tool_results)
        if n_to_trim == 0:
            return 0

        trimmed = 0
        for pos in tool_result_positions[:n_to_trim]:
            new_content = []
            for block in messages[pos]["content"]:
                # Preserve tool_use_id but shorten content text drastically
                orig_text = block.get("content", "")
                if isinstance(orig_text, str) and len(orig_text) > 200:
                    new_content.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id"),
                        "content": (
                            f"[old tool_result trimmed to save context — original "
                            f"was {len(orig_text)} chars. If you need this data, "
                            f"re-run the tool with the same parameters.]"
                        ),
                    })
                    trimmed += 1
                else:
                    new_content.append(block)
            messages[pos]["content"] = new_content
        return trimmed

    async def get_tools_list(self) -> List[Dict[str, Any]]:
        """Get list of tool definitions for API call."""
        return self.tool_definitions

    async def process_message(
        self,
        question: str,
        user_context: Dict[str, Any],
        system_prompt: str,
        history: List[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """Process user message and get response from Claude.

        progress_callback(stage, detail) is called at each phase change so
        the caller can show 'thinking → fetching data → formatting' updates
        to the user. Stages: 'thinking', 'tool', 'formatting'.
        """
        start_time = time.time()

        async def _emit(stage: str, detail: str = ""):
            if progress_callback:
                try:
                    await progress_callback(stage, detail)
                except Exception as e:
                    logger.warning("Progress callback failed", error=str(e))

        await _emit("thinking", "")

        messages = history or []
        messages.append({
            "role": "user",
            "content": question
        })

        # Select model based on question
        model = await router.route(question)
        self.client.default_model = model

        # Get tools list
        tools_list = await self.get_tools_list()

        iteration = 0
        response = None
        tools_called = []
        total_credits = 0.0
        total_input_tokens = 0
        # Logical model name from router (e.g. claude-sonnet-4-6) — may not
        # match the model that actually served (DeepSeek substitutes its own).
        # Seed actual_model from the provider config so audit_log is correct
        # even if the very first call fails before we see a response.model.
        provider = (settings.llm_provider or "deepseek").lower()
        if provider == "kie":
            actual_model = model
        else:
            actual_model = settings.deepseek_model or model

        trim_at = int(self.max_input_tokens * self.trim_threshold_ratio)

        while iteration < self.max_iterations:
            iteration += 1

            # Context-shrinking: if cumulative input is already 60% of the
            # limit, trim old tool_results to placeholders before the next
            # round so we don't blow through L1 circuit breaker.
            if total_input_tokens > trim_at:
                trimmed = self._trim_old_tool_results(messages)
                if trimmed:
                    logger.info(
                        "Trimmed old tool_results to fit context",
                        trimmed=trimmed,
                        total_input_tokens=total_input_tokens,
                        trim_at=trim_at,
                    )

            try:
                response = await self.client.send_message(
                    messages=messages,
                    system=system_prompt,
                    tools=tools_list if tools_list else None,
                    model=model
                )
                total_credits += float(response.get("credits_consumed", 0) or 0)
                total_input_tokens += int(response.get("usage", {}).get("input_tokens", 0))
                actual_model = response.get("model") or actual_model

                # CIRCUIT BREAKER L1 — per-request token/credit/tool-call ceiling.
                # Triggered after Claude has actually been called, so we know
                # the real cost so far. Prevents runaway costs from a single
                # bad question (worst case we've seen: 83 cr in 6 iterations).
                # Tool-call counter catches fan-out cases (e.g. 14× get_card_comments
                # for one "analyze refusals" question) before they burn the budget.
                tripped_reason = None
                if total_input_tokens > self.max_input_tokens:
                    tripped_reason = "input_tokens"
                elif total_credits > self.max_credits:
                    tripped_reason = "credits"
                elif len(tools_called) >= self.max_tool_calls:
                    tripped_reason = "tool_calls"

                if tripped_reason:
                    logger.warning(
                        "Circuit breaker tripped (per-request limit)",
                        reason=tripped_reason,
                        iteration=iteration,
                        total_input_tokens=total_input_tokens,
                        total_credits=total_credits,
                        tool_calls_count=len(tools_called),
                        max_input_tokens=self.max_input_tokens,
                        max_credits=self.max_credits,
                        max_tool_calls=self.max_tool_calls,
                        tools_called=tools_called,
                    )
                    return {
                        "answer": (
                            "К сожалению, не удалось собрать ответ на этот вопрос за разумное число шагов. "
                            "Попробуйте сформулировать запрос конкретнее — например, добавьте период, "
                            "конкретную воронку или сузьте критерий поиска."
                        ),
                        "error": f"circuit_breaker_per_request:{tripped_reason}",
                        "model": actual_model,
                        "iterations": iteration,
                        "tools_called": tools_called,
                        "usage": response.get("usage", {}),
                        "credits_consumed": total_credits,
                        "input_tokens_total": total_input_tokens,
                        "duration_ms": int((time.time() - start_time) * 1000),
                    }
            except Exception as e:
                logger.error("Claude API error", error=str(e), iteration=iteration)
                return {
                    "answer": f"Ошибка при обращении к ИИ: {str(e)}",
                    "error": str(e),
                    "model": actual_model,
                    "iterations": iteration,
                    "tools_called": tools_called,
                    "usage": {},
                    "credits_consumed": total_credits,
                    "duration_ms": int((time.time() - start_time) * 1000)
                }

            # Check if Claude wants to use tools
            # Handle both "end_turn" and None (both mean response is complete)
            if response["stop_reason"] in ("end_turn", None):
                await _emit("formatting", "")
                # Final response
                logger.info(
                    "Processing final response",
                    content_count=len(response.get("content", [])),
                    content_types=[type(b).__name__ for b in response.get("content", [])],
                    content_str=str(response.get("content", []))[:200]
                )

                answer_block = next(
                    (block for block in response["content"] if hasattr(block, "text")),
                    None
                )
                answer = answer_block.text if answer_block else "Ошибка: нет ответа"

                return {
                    "answer": answer,
                    "model": actual_model,
                    "iterations": iteration,
                    "tools_called": tools_called,
                    "usage": response.get("usage", {}),
                    "credits_consumed": total_credits,
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "stop_reason": response.get("stop_reason", "end_turn")
                }

            elif response["stop_reason"] == "tool_use":
                # Claude wants to use tools
                tool_results = []

                for block in response["content"]:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name = block.name
                        tool_input = block.input

                        tools_called.append(tool_name)
                        await _emit("tool", tool_name)

                        try:
                            result = await handlers.handle_tool(tool_name, tool_input, user_context)

                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result, ensure_ascii=False, indent=2)
                            })
                            logger.info("Tool executed successfully", tool=tool_name, iteration=iteration)
                        except Exception as e:
                            logger.error("Tool execution failed", tool=tool_name, error=str(e), iteration=iteration)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"Ошибка при выполнении инструмента: {str(e)}"
                            })

                # Convert SDK blocks to plain dicts for JSON-serialization
                assistant_content = []
                for block in response["content"]:
                    if hasattr(block, "type") and block.type == "tool_use":
                        assistant_content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
                    elif hasattr(block, "text"):
                        assistant_content.append({
                            "type": "text",
                            "text": block.text,
                        })

                messages.append({
                    "role": "assistant",
                    "content": assistant_content
                })

                # Add tool results
                messages.append({
                    "role": "user",
                    "content": tool_results
                })

            else:
                # Unknown stop_reason - try to extract answer and return
                logger.warning(
                    "Unknown stop reason, treating as end_turn",
                    stop_reason=response.get("stop_reason"),
                    iteration=iteration,
                    content_count=len(response.get("content", [])),
                    response_keys=list(response.keys())
                )

                answer_block = next(
                    (block for block in response.get("content", []) if hasattr(block, "text")),
                    None
                )
                answer = answer_block.text if answer_block else "Ошибка: нет ответа"

                return {
                    "answer": answer,
                    "model": actual_model,
                    "iterations": iteration,
                    "tools_called": tools_called,
                    "usage": response.get("usage", {}),
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "stop_reason": response.get("stop_reason", "unknown")
                }

        # Max iterations reached - try to get final response without tools
        logger.warning("Max iterations reached, requesting final response", iteration=iteration, tools_called=tools_called)

        # Without tools, some models (notably DeepSeek) will print tool calls
        # as raw text (DSML markup) when they "want" to call something but
        # the API rejects it. Tell the model explicitly to give a text-only
        # answer based on the data already collected.
        no_tools_system = (
            system_prompt
            + "\n\n=== ВАЖНО ===\n"
            "Достигнут лимит обращений к инструментам. БОЛЬШЕ НЕ ВЫЗЫВАЙ ИНСТРУМЕНТЫ — "
            "ни через tool_calls, ни в текстовом виде (никаких <invoke>, <tool_calls>, <function>, DSML).\n"
            "На основе данных, которые УЖЕ собраны в предыдущих сообщениях, "
            "дай человеку короткий и понятный текстовый ответ. Если данных явно "
            "недостаточно — честно скажи это одним абзацем."
        )

        try:
            final_response = await self.client.send_message(
                messages=messages,
                system=no_tools_system,
                model=model,
                tools=None,
                max_tokens=1024
            )

            answer_block = next(
                (block for block in final_response["content"] if hasattr(block, "text")),
                None
            )
            answer = answer_block.text if answer_block else "Ошибка: нет ответа"
        except Exception as e:
            logger.error("Failed to get final response", error=str(e))
            answer = "Ошибка при получении финального ответа. Достигнут лимит операций."
            final_response = {"usage": {}}

        return {
            "answer": answer,
            "error": "Max iterations reached",
            "model": actual_model,
            "iterations": iteration,
            "tools_called": tools_called,
            "usage": final_response.get("usage", {}),
            "duration_ms": int((time.time() - start_time) * 1000)
        }


# Global orchestrator instance
orchestrator = Orchestrator()
