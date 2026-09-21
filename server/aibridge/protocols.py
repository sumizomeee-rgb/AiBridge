from __future__ import annotations

import html
import json
import re
import time
import uuid
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


def _compact_text(text: str, max_chars: int) -> str:
    normalized = re.sub(r"[ \t]+", " ", text.replace("\x00", "")).strip()
    if len(normalized) <= max_chars:
        return normalized
    marker = "\n...[中间内容由网关压缩]...\n"
    available = max(0, max_chars - len(marker))
    head = available * 2 // 3
    tail = available - head
    tail_text = normalized[-tail:].lstrip() if tail else ""
    return normalized[:head].rstrip() + marker + tail_text


def _compact_schema(schema: Any, depth: int = 0) -> dict[str, Any]:
    """保留工具参数结构，压缩网页模型不需要的冗长描述。"""
    if not isinstance(schema, dict) or depth > 4:
        return {"type": "object"} if depth == 0 else {}
    result: dict[str, Any] = {}
    for key in ("type", "format", "default", "additionalProperties"):
        if key in schema:
            result[key] = schema[key]
    if schema.get("description"):
        result["description"] = _compact_text(str(schema["description"]), 180)
    if isinstance(schema.get("enum"), list):
        result["enum"] = schema["enum"][:30]
    if isinstance(schema.get("required"), list):
        result["required"] = schema["required"]
    if isinstance(schema.get("properties"), dict):
        result["properties"] = {
            str(name): _compact_schema(value, depth + 1)
            for name, value in schema["properties"].items()
        }
    if isinstance(schema.get("items"), dict):
        result["items"] = _compact_schema(schema["items"], depth + 1)
    for key in ("oneOf", "anyOf"):
        if isinstance(schema.get(key), list):
            result[key] = [_compact_schema(item, depth + 1) for item in schema[key][:8]]
    return result or {"type": schema.get("type", "object")}


def _compact_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(tool.get("name") or "tool"),
            "description": _compact_text(str(tool.get("description") or ""), 260),
            "input_schema": _compact_schema(tool.get("input_schema") or {"type": "object"}),
        }
        for tool in tools
    ]


def _minimal_schema(schema: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(schema, dict) or depth > 4:
        return {}
    result: dict[str, Any] = {}
    if schema.get("type"):
        result["type"] = schema["type"]
    if isinstance(schema.get("enum"), list):
        result["enum"] = schema["enum"][:30]
    if isinstance(schema.get("required"), list):
        result["required"] = schema["required"]
    if isinstance(schema.get("properties"), dict):
        result["properties"] = {
            str(name): _minimal_schema(value, depth + 1)
            for name, value in schema["properties"].items()
        }
    if isinstance(schema.get("items"), dict):
        result["items"] = _minimal_schema(schema["items"], depth + 1)
    return result or {"type": "object"}


def _tools_prompt_json(tools: list[dict[str, Any]], max_chars: int = 14000) -> str:
    compact = _compact_tools(tools)
    value = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(value) <= max_chars:
        return value
    minimal = [
        {
            "name": str(tool.get("name") or "tool"),
            "description": _compact_text(str(tool.get("description") or ""), 100),
            "input_schema": _minimal_schema(tool.get("input_schema") or {"type": "object"}),
        }
        for tool in tools
    ]
    value = json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
    if len(value) <= max_chars:
        return value
    signatures = []
    for tool in tools:
        schema = tool.get("input_schema") or {}
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        signatures.append(
            {
                "name": str(tool.get("name") or "tool"),
                "arguments": {
                    str(name): (value.get("type", "any") if isinstance(value, dict) else "any")
                    for name, value in (properties or {}).items()
                },
                "required": schema.get("required", []) if isinstance(schema, dict) else [],
            }
        )
    return json.dumps(signatures, ensure_ascii=False, separators=(",", ":"))


def _tool_policy(tool_choice: Any) -> str:
    if tool_choice == "none" or isinstance(tool_choice, dict) and tool_choice.get("type") == "none":
        return "禁止调用工具；直接回答。"
    if tool_choice == "required" or isinstance(tool_choice, dict) and tool_choice.get("type") == "any":
        return "本轮必须调用至少一个可用工具。"
    if isinstance(tool_choice, dict):
        name = tool_choice.get("name") or (tool_choice.get("function") or {}).get("name")
        if name:
            return f"本轮必须调用且只能调用工具 {name}。"
    return "按需调用工具；无需工具时直接回答。"


def web_tool_bridge_enabled(req: CanonicalRequest) -> bool:
    return bool(req.tools) and _tool_policy(req.tool_choice) != "禁止调用工具；直接回答。"


def _history_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if message.get("role") == "tool":
        return (
            "<tool_result id="
            + json.dumps(str(message.get("tool_call_id", "")), ensure_ascii=False)
            + ">\n"
            + _text_content(content)
            + "\n</tool_result>"
        )
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif not isinstance(block, dict):
                continue
            elif block.get("type") in {"text", "input_text", "output_text"}:
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                parts.append(
                    "<tool_call_history>"
                    + json.dumps(
                        {
                            "id": block.get("id", ""),
                            "name": block.get("name", "tool"),
                            "arguments": block.get("input") or {},
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "</tool_call_history>"
                )
            elif block.get("type") == "tool_result":
                parts.append(
                    "<tool_result id="
                    + json.dumps(str(block.get("tool_use_id", "")), ensure_ascii=False)
                    + ">\n"
                    + _text_content(block.get("content"))
                    + "\n</tool_result>"
                )
    else:
        text = _text_content(content)
        if text:
            parts.append(text)

    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        arguments: Any = fn.get("arguments") or "{}"
        try:
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            pass
        parts.append(
            "<tool_call_history>"
            + json.dumps(
                {"id": call.get("id", ""), "name": fn.get("name", "tool"), "arguments": arguments},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "</tool_call_history>"
        )
    return "\n".join(part for part in parts if part)


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


def flatten_web_prompt(req: CanonicalRequest) -> str:
    """把 API 对话无损展开为网页提示，并为 Web 模型注入精简工具协议。

    网页来源只有一个 prompt 文本通道，因此角色边界需要用标签表达；但请求正文、
    system 和历史消息不能在网关内静默裁剪。上游若无法容纳完整输入，应返回明确错误。
    """
    sections: list[str] = []
    if req.system:
        sections.append(
            "<aibridge_system_constraints>\n"
            + req.system
            + "\n</aibridge_system_constraints>"
        )
    if web_tool_bridge_enabled(req):
        tools_json = _tools_prompt_json(req.tools)
        sections.append(
            "<aibridge_tool_protocol>\n"
            "你是 API Agent 的模型核心。不得假装已经执行工具，也不得编造工具结果。\n"
            f"策略：{_tool_policy(req.tool_choice)}\n"
            "需要工具时，每次调用严格输出一个标签，可连续输出多个标签：\n"
            '<aibridge_tool_call>{"id":"call_可选","name":"工具名","arguments":{"参数":"值"}}</aibridge_tool_call>\n'
            "arguments 必须是 JSON 对象，name 必须来自可用工具。调用工具时不要在标签外解释；"
            "收到 <tool_result> 后继续完成任务。最终答案不得包含工具调用标签。\n"
            "可用工具："
            + tools_json
            + "\n</aibridge_tool_protocol>"
        )

    latest_input_index = next(
        (
            index
            for index in range(len(req.messages) - 1, -1, -1)
            if req.messages[index].get("role") in {"user", "tool"}
        ),
        None,
    )
    for index, message in enumerate(req.messages):
        role = {"user": "用户", "assistant": "助手", "tool": "工具结果"}.get(message.get("role"), "消息")
        text = _history_content(message)
        if not text:
            continue
        if index == latest_input_index:
            sections.append(f"<aibridge_current_input>\n{text}\n</aibridge_current_input>")
        else:
            sections.append(f'<aibridge_message role="{role}">\n{text}\n</aibridge_message>')
    return "\n\n".join(sections).strip()


_WEB_TOOL_TAG = re.compile(
    r"<(?:aibridge_)?tool_call>\s*(.*?)\s*</(?:aibridge_)?tool_call>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_MARKER = r"(?:\|\||｜｜)DSML(?:\|\||｜｜)"
_DSML_CALLS = re.compile(
    rf"<\s*{_DSML_MARKER}\s+calls\s*>(.*?)</\s*{_DSML_MARKER}\s+calls\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_INVOKE = re.compile(
    rf"<\s*{_DSML_MARKER}\s+invoke\b([^>]*)>(.*?)</\s*{_DSML_MARKER}\s+invoke\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_PARAMETER = re.compile(
    rf"<\s*{_DSML_MARKER}\s+parameter\b([^>]*)>(.*?)</\s*{_DSML_MARKER}\s+parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)
_XML_ATTRIBUTE = re.compile(r"([\w:-]+)\s*=\s*([\"'])(.*?)\2", re.DOTALL)


def _decode_tool_payload(raw: str) -> list[dict[str, Any]]:
    value = raw.strip()
    if value.startswith("```") and value.endswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE | re.DOTALL)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
        return [item for item in payload["tool_calls"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return [payload] if isinstance(payload, dict) else []


def _tool_call_from_payload(payload: dict[str, Any], allowed: set[str]) -> dict[str, Any] | None:
    fn = payload.get("function") if isinstance(payload.get("function"), dict) else payload
    name = str(fn.get("name") or "")
    if name not in allowed:
        return None
    arguments = fn.get("arguments", payload.get("arguments", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    return {
        "id": str(payload.get("id") or f"call_{uuid.uuid4().hex[:16]}"),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _attributes(raw: str) -> dict[str, str]:
    return {match.group(1): html.unescape(match.group(3)) for match in _XML_ATTRIBUTE.finditer(raw)}


def _decode_dsml_calls(raw: str, allowed: set[str]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for invoke in _DSML_INVOKE.finditer(raw):
        invoke_attrs = _attributes(invoke.group(1))
        name = invoke_attrs.get("name", "")
        arguments: dict[str, Any] = {}
        for parameter in _DSML_PARAMETER.finditer(invoke.group(2)):
            attrs = _attributes(parameter.group(1))
            param_name = attrs.get("name")
            if not param_name:
                continue
            value: Any = html.unescape(parameter.group(2)).strip()
            if attrs.get("string", "").lower() != "true":
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            arguments[param_name] = value
        call = _tool_call_from_payload(
            {"id": invoke_attrs.get("id"), "name": name, "arguments": arguments},
            allowed,
        )
        if call:
            calls.append(call)
    return calls


def parse_web_tool_response(
    text: str,
    tools: list[dict[str, Any]],
    dialect: str = "standard",
) -> tuple[str, list[dict[str, Any]]]:
    """按来源方言把网页回复解析为内部 OpenAI 形态的工具调用。"""
    if not tools:
        return text, []
    allowed = {str(tool.get("name")) for tool in tools if tool.get("name")}
    parsed: list[dict[str, Any]] = []
    consumed: list[tuple[int, int]] = []
    for match in _WEB_TOOL_TAG.finditer(text):
        payloads = _decode_tool_payload(match.group(1))
        accepted = False
        for payload in payloads:
            call = _tool_call_from_payload(payload, allowed)
            if call:
                parsed.append(call)
                accepted = True
        if accepted:
            consumed.append(match.span())

    if dialect == "deepseek":
        for match in _DSML_CALLS.finditer(text):
            calls = _decode_dsml_calls(match.group(1), allowed)
            if calls:
                parsed.extend(calls)
                consumed.append(match.span())

        if not parsed:
            calls = _decode_dsml_calls(text, allowed)
            if calls:
                parsed.extend(calls)
                consumed.extend(match.span() for match in _DSML_INVOKE.finditer(text))

    if not parsed:
        stripped = text.strip()
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE | re.DOTALL)
        payloads = _decode_tool_payload(candidate)
        if payloads and candidate.startswith("{") and '"tool_calls"' in candidate:
            synthetic = "".join(
                f"<aibridge_tool_call>{json.dumps(item, ensure_ascii=False)}</aibridge_tool_call>"
                for item in payloads
            )
            return parse_web_tool_response(synthetic, tools, dialect)

    visible = text
    for start, end in reversed(consumed):
        visible = visible[:start] + visible[end:]
    visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
    return visible, parsed


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
        if item.type == "keepalive":
            yield b": aibridge-queued\n\n"
        elif item.type == "text" and item.text:
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
        if item.type == "keepalive":
            yield b": aibridge-queued\n\n"
        elif item.type == "text" and item.text:
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
