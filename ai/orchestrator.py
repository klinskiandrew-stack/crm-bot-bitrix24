from typing import List, Dict, Any, Optional
import json
import time
import structlog
from ai.client import KieAIClient
from ai.router import router
from ai.tools import get_tools_definitions
from ai.tool_handlers import handlers

logger = structlog.get_logger()


class Orchestrator:
    """Manage conversation with Claude including function calling."""

    def __init__(self):
        self.client = KieAIClient()
        self.max_iterations = 5
        self.tool_definitions = get_tools_definitions()

    async def get_tools_list(self) -> List[Dict[str, Any]]:
        """Get list of tool definitions for API call."""
        return self.tool_definitions

    async def process_message(
        self,
        question: str,
        user_context: Dict[str, Any],
        system_prompt: str,
        history: List[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Process user message and get response from Claude."""
        start_time = time.time()

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

        while iteration < self.max_iterations:
            iteration += 1

            try:
                response = await self.client.send_message(
                    messages=messages,
                    system=system_prompt,
                    tools=tools_list if tools_list else None,
                    model=model
                )
                total_credits += float(response.get("credits_consumed", 0) or 0)
            except Exception as e:
                logger.error("Claude API error", error=str(e), iteration=iteration)
                return {
                    "answer": f"Ошибка при обращении к ИИ: {str(e)}",
                    "error": str(e),
                    "model": model,
                    "iterations": iteration,
                    "tools_called": tools_called,
                    "usage": {},
                    "credits_consumed": total_credits,
                    "duration_ms": int((time.time() - start_time) * 1000)
                }

            # Check if Claude wants to use tools
            # Handle both "end_turn" and None (both mean response is complete)
            if response["stop_reason"] in ("end_turn", None):
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
                    "model": model,
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
                    "model": model,
                    "iterations": iteration,
                    "tools_called": tools_called,
                    "usage": response.get("usage", {}),
                    "duration_ms": int((time.time() - start_time) * 1000),
                    "stop_reason": response.get("stop_reason", "unknown")
                }

        # Max iterations reached - try to get final response without tools
        logger.warning("Max iterations reached, requesting final response", iteration=iteration, tools_called=tools_called)

        # Request final response without tools
        try:
            final_response = await self.client.send_message(
                messages=messages,
                system=system_prompt,
                model=model,
                tools=None,  # No tools for final response
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
            "model": model,
            "iterations": iteration,
            "tools_called": tools_called,
            "usage": final_response.get("usage", {}),
            "duration_ms": int((time.time() - start_time) * 1000)
        }


# Global orchestrator instance
orchestrator = Orchestrator()
