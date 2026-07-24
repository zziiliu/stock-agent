"""Small helpers for streaming LangGraph ReAct agent output."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessageChunk, BaseMessage


def chunk_text_content(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def tool_call_chunk_value(call_chunk: Any, key: str) -> Any:
    if isinstance(call_chunk, dict):
        return call_chunk.get(key)
    return getattr(call_chunk, key, None)


def progress_for_tool(tool_name: str, messages: dict[str, str] | None = None) -> str:
    if messages and tool_name in messages:
        return messages[tool_name]
    return f"正在调用工具：{tool_name}"


async def stream_react_agent_chunks(
    agent: Any,
    input_data: dict[str, Any],
    *,
    agent_id: str,
    streamed_parts: list[str],
    tool_progress_messages: dict[str, str] | None = None,
    recursion_limit: int = 50,
) -> AsyncIterator[dict[str, Any]]:
    """Yield token/progress events from a LangGraph ReAct agent astream."""

    sent_tool_names: set[str] = set()
    tool_call_count = 0

    async for message_chunk, metadata in agent.astream(
        input_data,
        config={"recursion_limit": recursion_limit},
        stream_mode="messages",
    ):
        if not isinstance(message_chunk, AIMessageChunk):
            continue

        content = chunk_text_content(message_chunk)
        if content:
            streamed_parts.append(content)
            yield {
                "event": "token",
                "data": {
                    "agent": agent_id,
                    "content": content,
                },
            }

        tool_call_chunks = getattr(message_chunk, "tool_call_chunks", None) or []
        for call_chunk in tool_call_chunks:
            tool_name = tool_call_chunk_value(call_chunk, "name") or ""
            if not tool_name or tool_name in sent_tool_names:
                continue

            sent_tool_names.add(tool_name)
            tool_call_count += 1
            print(f"\n[TOOL CALL #{tool_call_count}] {agent_id}: {tool_name}", flush=True)
            yield {
                "event": "agent_progress",
                "data": {
                    "agent": agent_id,
                    "message": progress_for_tool(tool_name, tool_progress_messages),
                },
            }
