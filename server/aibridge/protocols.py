from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class CanonicalRequest:
    model: str
    upstream_model: str
    system: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 4096
    temperature: float | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: Any = None


@dataclass
class CanonicalEvent:
    type: str
    text: str = ""
    reasoning: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None
    tool: dict[str, Any] | None = None


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"}:
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict) and block.get("type") == "tool_result":
                parts.append(f"[工具结果]\n{_text_content(block.get('content'))}")
        return "\n".join(x for x in parts if x)
    return "" if content is None else str(content)


def from_openai(body: dict[str, Any], upstream_model: str) -> CanonicalRequest:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in body.get("messages") or []:
        role = message.get("role", "user")
        if role in {"system", "developer"}:
            system_parts.append(_text_content(message.get("content")))
        else:
            messages.append({"role": role, "content": message.get("content", ""), "tool_calls": message.get("tool_calls"), "tool_call_id": message.get("tool_call_id")})
    tools = []
    for item in body.get("tools") or []:
        fn = item.get("function", item)
        tools.append({"name": fn.get("name", "tool"), "description": fn.get("description", ""), "input_schema": fn.get("parameters", {"type": "object", "properties": {}})})
    return CanonicalRequest(
        model=body.get("model", ""), upstream_model=upstream_model,
        system="\n\n".join(x for x in system_parts if x), messages=messages,
        max_tokens=int(body.get("max_completion_tokens") or body.get("max_tokens") or 4096),
        temperature=body.get("temperature"), tools=tools, tool_choice=body.get("tool_choice"),
    )


def from_anthropic(body: dict[str, Any], upstream_model: str) -> CanonicalRequest:
    system = _text_content(body.get("system", ""))
    tools = [{"name": x.get("name", "tool"), "description": x.get("description", ""), "input_schema": x.get("input_schema", {"type": "object", "properties": {}})} for x in body.get("tools") or []]
    return CanonicalRequest(
        model=body.get("model", ""), upstream_model=upstream_model, system=system,
        messages=[dict(x) for x in body.get("messages") or []], max_tokens=int(body.get("max_tokens") or 4096),
        temperature=body.get("temperature"), tools=tools, tool_choice=body.get("tool_choice"),
    )


def to_openai_upstream(req: CanonicalRequest, stream: bool = True) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if req.system:
        messages.append({"role": "system", "content": req.system})
    for item in req.messages:
        message = {"role": item.get("role", "user"), "content": item.get("content", "")}
        if item.get("tool_calls"):
            message["tool_calls"] = item["tool_calls"]
        if item.get("tool_call_id"):
            message["tool_call_id"] = item["tool_call_id"]
        messages.append(message)
    body: dict[str, Any] = {"model": req.upstream_model, "messages": messages, "stream": stream, "max_tokens": req.max_tokens}
    if stream:
        body["stream_options"] = {"include_usage": True}
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.tools:
        body["tools"] = [{"type": "function", "function": {"name": x["name"], "description": x.get("description", ""), "parameters": x.get("input_schema", {})}} for x in req.tools]
    if req.tool_choice is not None:
        body["tool_choice"] = req.tool_choice
    return body


def to_anthropic_upstream(req: CanonicalRequest, stream: bool = True) -> dict[str, Any]:
    messages = []
    for item in req.messages:
        role = item.get("role", "user")
        if role == "tool":
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": item.get("tool_call_id", ""), "content": _text_content(item.get("content"))}]})
        elif role in {"user", "assistant"}:
            messages.append({"role": role, "content": item.get("content", "")})
    body: dict[str, Any] = {"model": req.upstream_model, "messages": messages, "max_tokens": req.max_tokens, "stream": stream}
    if req.system:
        body["system"] = req.system
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.tools:
        body["tools"] = req.tools
    if isinstance(req.tool_choice, dict) and req.tool_choice.get("type") in {"auto", "any", "tool", "none"}:
        body["tool_choice"] = req.tool_choice
    return body


def flatten_prompt(req: CanonicalRequest) -> str:
    parts = []
    if req.system:
        parts.append(f"系统要求：\n{req.system}")
    for message in req.messages:
        role = {"user": "用户", "assistant": "助手", "tool": "工具"}.get(message.get("role"), message.get("role", "消息"))
        text = _text_content(message.get("content"))
        if text:
            parts.append(f"{role}：\n{text}")
    if req.tools:
        parts.append("可用工具（若需调用，请输出兼容的工具调用 JSON）：\n" + json.dumps(req.tools, ensure_ascii=False))
    return "\n\n".join(parts)


def flatten_web_prompt(req: CanonicalRequest, max_chars: int = 32000) -> str:
    """把对话压成适合官网聊天框的文本，不转发 Agent 内部控制提示。"""
    parts = []
    for message in req.messages[-24:]:
        role = {"user": "用户", "assistant": "助手", "tool": "工具结果"}.get(message.get("role"), "消息")
        text = _text_content(message.get("content"))
        if text:
            parts.append(f"{role}：\n{text}")
    prompt = "\n\n".join(parts).strip()
    if not prompt and req.system:
        prompt = req.system.strip()
    if len(prompt) > max_chars:
        prompt = "[较早的对话内容已由网关省略]\n\n" + prompt[-max_chars:]
    return prompt


async def collect_events(events: AsyncIterator[CanonicalEvent]) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
    text_parts: list[str] = []
    usage: dict[str, int] = {}
    tools: list[dict[str, Any]] = []
    async for event in events:
        if event.type == "text":
            text_parts.append(event.text)
        elif event.type == "usage":
            usage.update(event.usage)
        elif event.type == "tool" and event.tool:
            tools.append(event.tool)
    return "".join(text_parts), usage, tools


def openai_response(request_id: str, model: str, text: str, usage: dict[str, int], tools: list[dict[str, Any]]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tools:
        message["tool_calls"] = tools
    prompt = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    completion = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return {
        "id": request_id, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tools else "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": usage.get("total_tokens", prompt + completion)},
    }


def anthropic_response(request_id: str, model: str, text: str, usage: dict[str, int], tools: list[dict[str, Any]]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for tool in tools:
        fn = tool.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"raw": fn.get("arguments", "")}
        content.append({"type": "tool_use", "id": tool.get("id", "tool"), "name": fn.get("name", "tool"), "input": args})
    return {
        "id": request_id, "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": "tool_use" if tools else "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": usage.get("input_tokens", usage.get("prompt_tokens", 0)), "output_tokens": usage.get("output_tokens", usage.get("completion_tokens", 0))},
    }


def sse(data: dict[str, Any], event: str | None = None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return (prefix + "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()


async def openai_sse(events: AsyncIterator[CanonicalEvent], request_id: str, model: str) -> AsyncIterator[bytes]:
    created = int(time.time())
    yield sse({"id": request_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
    usage: dict[str, int] = {}
    tools: list[dict[str, Any]] = []
    async for item in events:
        if item.type == "text" and item.text:
            yield sse({"id": request_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"content": item.text}, "finish_reason": None}]})
        elif item.type == "reasoning" and item.reasoning:
            yield sse({"id": request_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"reasoning_content": item.reasoning}, "finish_reason": None}]})
        elif item.type == "usage":
            usage.update(item.usage)
        elif item.type == "tool" and item.tool:
            tools.append(item.tool)
    if tools:
        yield sse({"id": request_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"tool_calls": [dict(tool) | {"index": index} for index, tool in enumerate(tools)]}, "finish_reason": None}]})
    yield sse({"id": request_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tools else "stop"}], "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0), "total_tokens": usage.get("total_tokens", 0)}})
    yield b"data: [DONE]\n\n"


async def anthropic_sse(events: AsyncIterator[CanonicalEvent], request_id: str, model: str) -> AsyncIterator[bytes]:
    usage: dict[str, int] = {}
    tools: list[dict[str, Any]] = []
    yield sse({"type": "message_start", "message": {"id": request_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}, "message_start")
    yield sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start")
    async for item in events:
        if item.type == "text" and item.text:
            yield sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": item.text}}, "content_block_delta")
        elif item.type == "usage":
            usage.update(item.usage)
        elif item.type == "tool" and item.tool:
            tools.append(item.tool)
    yield sse({"type": "content_block_stop", "index": 0}, "content_block_stop")
    for offset, tool in enumerate(tools, 1):
        fn = tool.get("function", {})
        raw_args = fn.get("arguments") or "{}"
        yield sse({"type": "content_block_start", "index": offset, "content_block": {"type": "tool_use", "id": tool.get("id", f"tool_{offset}"), "name": fn.get("name", "tool"), "input": {}}}, "content_block_start")
        yield sse({"type": "content_block_delta", "index": offset, "delta": {"type": "input_json_delta", "partial_json": raw_args}}, "content_block_delta")
        yield sse({"type": "content_block_stop", "index": offset}, "content_block_stop")
    yield sse({"type": "message_delta", "delta": {"stop_reason": "tool_use" if tools else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": usage.get("output_tokens", 0)}}, "message_delta")
    yield sse({"type": "message_stop"}, "message_stop")
