from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except (ImportError, OSError):
    CurlAsyncSession = None

from .protocols import CanonicalEvent, CanonicalRequest, flatten_web_prompt, parse_web_tool_response, to_anthropic_upstream, to_openai_upstream, web_tool_bridge_enabled
from .relay import RelayError, relay_broker


logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: str = "upstream_error", http_status: int = 502):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


def endpoint(base_url: str, resource: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith(f"/{resource}"):
        return base
    if base.endswith("/v1"):
        return f"{base}/{resource}"
    return f"{base}/v1/{resource}"


def classify_http(status_code: int, text: str = "") -> ProviderError:
    short = re.sub(r"\s+", " ", text).strip()[:240]
    if status_code in {401, 403}:
        label = "鉴权失效" if status_code == 401 else "被上游拒绝或触发风控"
        return ProviderError(f"{label}（HTTP {status_code}）{': ' + short if short else ''}", "auth_expired" if status_code == 401 else "challenge", 502)
    if status_code == 429:
        return ProviderError(f"上游限流（HTTP 429）{': ' + short if short else ''}", "rate_limited", 429)
    return ProviderError(f"上游返回 HTTP {status_code}{': ' + short if short else ''}", "upstream_error", 502)


def parse_curl(text: str) -> dict[str, Any]:
    normalized = text.replace("^\r\n", " ").replace("^\n", " ").replace("\\\r\n", " ").replace("\\\n", " ")
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as exc:
        raise ProviderError(f"cURL 无法解析：{exc}", "invalid_config", 400) from exc
    url = ""
    headers: dict[str, str] = {}
    cookie = ""
    body = ""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("http://") or token.startswith("https://"):
            url = token
        elif token in {"-H", "--header"} and i + 1 < len(tokens):
            i += 1
            raw = tokens[i]
            if ":" in raw:
                name, value = raw.split(":", 1)
                if name.lower() not in {"content-length", "host", ":authority", ":method", ":path", ":scheme"}:
                    headers[name.strip()] = value.strip()
        elif token in {"-b", "--cookie"} and i + 1 < len(tokens):
            i += 1
            cookie = tokens[i]
        elif token in {"-d", "--data", "--data-raw", "--data-binary"} and i + 1 < len(tokens):
            i += 1
            body = _decode_curl_data(tokens[i])
        i += 1
    for key in list(headers):
        if key.lower() == "cookie":
            cookie = headers.pop(key)
    if not url:
        raise ProviderError("cURL 中没有找到请求 URL", "invalid_config", 400)
    return {"request_url": url, "headers": headers, "cookie": cookie, "body": body}


def _decode_curl_data(value: str) -> str:
    """Decode the small ANSI-C escape subset emitted by Chrome's Copy as cURL."""
    if not value.startswith("$"):
        return value
    value = value[1:]
    escapes = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'"}

    def replace(match: re.Match[str]) -> str:
        code = match.group(1)
        if code in escapes:
            return escapes[code]
        if code.startswith("x"):
            return chr(int(code[1:], 16))
        if code.startswith("u") or code.startswith("U"):
            return chr(int(code[1:], 16))
        if code[0].isdigit():
            return chr(int(code, 8))
        return code

    return re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-7]{1,3}|.)", replace, value)


def _api_headers(source: dict[str, Any]) -> dict[str, str]:
    config = source.get("config", {})
    secret = source.get("credential", {})
    token = secret.get("api_key", "")
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    if source["protocol"] == "anthropic":
        headers["anthropic-version"] = config.get("anthropic_version", "2023-06-01")
        if config.get("auth_mode") == "x-api-key":
            headers["x-api-key"] = token
        else:
            headers["authorization"] = f"Bearer {token}"
    else:
        headers["authorization"] = f"Bearer {token}"
    headers.update(config.get("headers") or {})
    return headers


async def _stream_openai(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    url = endpoint(source["base_url"], "chat/completions")
    timeout = httpx.Timeout(20, read=180, write=30, pool=10)
    tool_parts: dict[int, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        async with client.stream("POST", url, headers=_api_headers(source), json=to_openai_upstream(req, True)) as response:
            if response.status_code >= 400:
                raise classify_http(response.status_code, (await response.aread()).decode(errors="replace"))
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if data.get("usage"):
                    yield CanonicalEvent("usage", usage=data["usage"])
                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield CanonicalEvent("text", text=delta["content"])
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        yield CanonicalEvent("reasoning", reasoning=reasoning)
                    for tool in delta.get("tool_calls") or []:
                        index = int(tool.get("index", 0))
                        part = tool_parts.setdefault(index, {"id": tool.get("id") or f"call_{uuid.uuid4().hex[:16]}", "type": "function", "function": {"name": "", "arguments": ""}})
                        fn = tool.get("function") or {}
                        part["function"]["name"] += fn.get("name", "")
                        part["function"]["arguments"] += fn.get("arguments", "")
    for _, tool in sorted(tool_parts.items()):
        yield CanonicalEvent("tool", tool=tool)


async def _stream_anthropic(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    url = endpoint(source["base_url"], "messages")
    timeout = httpx.Timeout(20, read=180, write=30, pool=10)
    tools: dict[int, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=timeout, http2=True) as client:
        async with client.stream("POST", url, headers=_api_headers(source), json=to_anthropic_upstream(req, True)) as response:
            if response.status_code >= 400:
                raise classify_http(response.status_code, (await response.aread()).decode(errors="replace"))
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")
                if kind == "message_start":
                    yield CanonicalEvent("usage", usage=data.get("message", {}).get("usage") or {})
                elif kind == "content_block_start" and data.get("content_block", {}).get("type") == "tool_use":
                    block = data["content_block"]
                    tools[int(data.get("index", 0))] = {"id": block.get("id"), "type": "function", "function": {"name": block.get("name", "tool"), "arguments": ""}}
                elif kind == "content_block_delta":
                    delta = data.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        yield CanonicalEvent("text", text=delta.get("text", ""))
                    elif delta.get("type") == "thinking_delta":
                        yield CanonicalEvent("reasoning", reasoning=delta.get("thinking", ""))
                    elif delta.get("type") == "input_json_delta":
                        tools.setdefault(int(data.get("index", 0)), {"id": f"tool_{uuid.uuid4().hex[:16]}", "type": "function", "function": {"name": "tool", "arguments": ""}})["function"]["arguments"] += delta.get("partial_json", "")
                elif kind == "message_delta":
                    yield CanonicalEvent("usage", usage=data.get("usage") or {})
    for _, tool in sorted(tools.items()):
        yield CanonicalEvent("tool", tool=tool)


def _web_headers(source: dict[str, Any]) -> tuple[str, dict[str, str]]:
    credential = source.get("credential") or {}
    request_url = credential.get("request_url", "")
    headers = {str(k): str(v) for k, v in (credential.get("headers") or {}).items()}
    cookie = credential.get("cookie", "")
    if cookie:
        headers["cookie"] = cookie
    for key in list(headers):
        if key.lower() in {"content-length", "host", "accept-encoding", "connection", ":authority", ":method", ":path", ":scheme"}:
            headers.pop(key)
    headers.setdefault("content-type", "application/json")
    headers.setdefault("accept", "text/event-stream")
    return request_url, headers


def _parse_longcat_stream(raw: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    text_parts: list[str] = []
    final_text = ""
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        event = data.get("event") or {}
        kind = event.get("type")
        if kind == "content" and isinstance(event.get("content"), str):
            chunk = event["content"]
            if chunk:
                text_parts.append(chunk)
                events.append(CanonicalEvent("text", text=chunk))
        elif kind == "finish":
            if isinstance(event.get("finalContentX"), str):
                final_text = event["finalContentX"]
            usage = event.get("usage") or data.get("tokenInfo") or {}
            if usage:
                events.append(CanonicalEvent("usage", usage={
                    "prompt_tokens": int(usage.get("inputTokens") or usage.get("promptTokens") or 0),
                    "completion_tokens": int(usage.get("outputTokens") or usage.get("completionTokens") or 0),
                    "total_tokens": int(usage.get("totalTokens") or 0),
                }))
    if not text_parts and final_text:
        events.insert(0, CanonicalEvent("text", text=final_text))
    return events


def _longcat_stream_diagnostic(raw: str) -> str:
    event_types: list[str] = []
    data_lines = 0
    malformed = 0
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        data_lines += 1
        try:
            data = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            malformed += 1
            continue
        event = data.get("event") if isinstance(data, dict) else None
        kind = event.get("type") if isinstance(event, dict) else None
        if isinstance(kind, str) and kind not in event_types:
            event_types.append(kind)
    details = [f"响应 {len(raw.encode('utf-8'))} 字节", f"SSE 数据 {data_lines} 条"]
    if event_types:
        details.append(f"事件类型 {','.join(event_types[:8])}")
    if malformed:
        details.append(f"无法解析 {malformed} 条")
    if data_lines == 0 and raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            details.append("非 JSON/SSE 响应")
        else:
            if isinstance(payload, dict):
                if payload.get("code") is not None:
                    details.append(f"code={str(payload['code'])[:40]}")
                if payload.get("message"):
                    details.append(f"message={str(payload['message'])[:120]}")
    return "；".join(details)


async def _stream_longcat(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    credential = source.get("credential") or {}
    if credential.get("browser_relay") is not True:
        raise ProviderError("LongCat 需要通过 AiBridge Catcher 完成浏览器中继配置", "unconfigured", 400)
    captured_headers = credential.get("headers") or {}
    relay_headers = {
        str(name): str(value) for name, value in captured_headers.items()
        if str(name).lower() in {"m-appkey", "x-client-language", "x-requested-with", "access-token"}
        and str(value) not in {"", "undefined", "null"}
    }
    try:
        response = await relay_broker.request(
            "web-longcat",
            "longcat_chat",
            {"prompt": flatten_web_prompt(req), "headers": relay_headers},
        )
    except RelayError as exc:
        raise ProviderError(str(exc), "browser_relay_unavailable", 503) from exc
    status = int(response.get("status") or 0)
    raw = str(response.get("body") or "")
    if status == 418:
        raise ProviderError(
            "LongCat 触发美团安全验证（HTTP 418）；请在 longcat.chat 页面完成验证后重试",
            "challenge",
            502,
        )
    if status >= 400:
        raise classify_http(status, raw)
    events = _parse_longcat_stream(raw)
    if not any(event.type in {"text", "reasoning", "tool"} for event in events):
        diagnostic = _longcat_stream_diagnostic(raw)
        logger.warning(
            "LongCat 响应无法解析：阶段=%s，HTTP=%s，%s",
            str(response.get("stage") or "未知")[:40],
            status,
            diagnostic,
        )
        raise ProviderError(f"LongCat 响应中没有可识别的文本（{diagnostic}）", "protocol_mismatch")
    for event in events:
        yield event


async def _curl_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, str]:
    if CurlAsyncSession is not None:
        session = CurlAsyncSession(impersonate="chrome", timeout=180)
        try:
            response = await session.post(url, headers=headers, json=body)
            return response.status_code, response.text
        finally:
            await session.close()
    timeout = httpx.Timeout(20, read=180, write=30, pool=10)
    async with httpx.AsyncClient(timeout=timeout, http2=True, follow_redirects=True) as client:
        response = await client.post(url, headers=headers, json=body)
        return response.status_code, response.text


def _connect_message(payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return b"\x00" + len(data).to_bytes(4, "big") + data


def _pop_connect_frame(buffer: bytearray) -> tuple[int, dict[str, Any] | None] | None:
    if len(buffer) < 5:
        return None
    flags = buffer[0]
    length = int.from_bytes(buffer[1:5], "big")
    if length > 8 * 1024 * 1024:
        raise ProviderError("Kimi Connect 响应帧超过 8 MiB", "protocol_mismatch")
    if len(buffer) < 5 + length:
        return None
    if flags & ~0x03:
        raise ProviderError(f"Kimi Connect 使用了不支持的帧标志：{flags}", "protocol_mismatch")
    if flags & 0x01:
        raise ProviderError("Kimi Connect 压缩帧暂不支持", "protocol_mismatch")
    payload = bytes(buffer[5:5 + length])
    del buffer[:5 + length]
    if not payload:
        return flags, None
    try:
        message = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("Kimi Connect 响应包含无效 JSON", "protocol_mismatch") from exc
    if not isinstance(message, dict):
        raise ProviderError("Kimi Connect 响应事件格式异常", "protocol_mismatch")
    return flags, message


def _kimi_delta(message: dict[str, Any]) -> CanonicalEvent | None:
    operation = str(message.get("op") or "")
    mask = str(message.get("mask") or "")
    block = message.get("block") or {}
    if not isinstance(block, dict):
        return None
    if operation == "set" and mask in {"block.text", "block.think"}:
        part = block.get("text" if mask == "block.text" else "think") or {}
    elif operation == "append" and mask in {"block.text.content", "block.think.content"}:
        part = block.get("text" if mask == "block.text.content" else "think") or {}
    else:
        return None
    content = part.get("content") if isinstance(part, dict) else None
    if not isinstance(content, str) or not content:
        return None
    return CanonicalEvent("reasoning", reasoning=content) if "think" in mask else CanonicalEvent("text", text=content)


def _perplexity_step_chunks(value: Any) -> tuple[list[str], str]:
    chunks: list[str] = []
    fallback = ""
    if isinstance(value, dict):
        payload = value.get("text_payload")
        if isinstance(payload, dict):
            raw_chunks = payload.get("chunks")
            if isinstance(raw_chunks, list):
                chunks.extend(item for item in raw_chunks if isinstance(item, str) and item)
            if isinstance(payload.get("text"), str) and payload["text"]:
                fallback = payload["text"]
        for nested in value.values():
            nested_chunks, nested_fallback = _perplexity_step_chunks(nested)
            chunks.extend(nested_chunks)
            fallback = nested_fallback or fallback
    elif isinstance(value, list):
        for nested in value:
            nested_chunks, nested_fallback = _perplexity_step_chunks(nested)
            chunks.extend(nested_chunks)
            fallback = nested_fallback or fallback
    return chunks, fallback


def _parse_perplexity_stream(raw: str) -> list[str]:
    chunks: list[str] = []
    markdown_chunks: dict[int, str] = {}
    fallback = ""
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        for block in event.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            markdown = block.get("markdown_block")
            if isinstance(markdown, dict):
                try:
                    start = int(markdown.get("chunk_starting_offset", len(markdown_chunks)))
                except (TypeError, ValueError):
                    start = len(markdown_chunks)
                for index, chunk in enumerate(markdown.get("chunks") or []):
                    if isinstance(chunk, str) and chunk:
                        markdown_chunks[start + index] = chunk
                if isinstance(markdown.get("answer"), str) and markdown["answer"]:
                    fallback = markdown["answer"]
            diff = block.get("diff_block")
            if isinstance(diff, dict) and diff.get("field") == "workflow_block":
                for patch in diff.get("patches") or []:
                    if not isinstance(patch, dict):
                        continue
                    path = str(patch.get("path") or "")
                    value = patch.get("value")
                    if "/text_payload/chunks/" in path and isinstance(value, str) and value:
                        chunks.append(value)
                    elif path.endswith("/text_payload/text") and isinstance(value, str) and value:
                        fallback = value
                    elif patch.get("op") == "add" and isinstance(value, (dict, list)):
                        initial_chunks, initial_fallback = _perplexity_step_chunks(value)
                        chunks.extend(initial_chunks)
                        fallback = initial_fallback or fallback
            workflow = block.get("workflow_block")
            if isinstance(workflow, dict):
                _, full_text = _perplexity_step_chunks(workflow)
                fallback = full_text or fallback
    if chunks:
        return chunks
    if markdown_chunks:
        return [markdown_chunks[index] for index in sorted(markdown_chunks)]
    return [fallback] if fallback else []


async def _curl_get(url: str, headers: dict[str, str]) -> tuple[int, str]:
    if CurlAsyncSession is not None:
        session = CurlAsyncSession(impersonate="chrome", timeout=30)
        try:
            response = await session.get(url, headers=headers)
            return response.status_code, response.text
        finally:
            await session.close()
    async with httpx.AsyncClient(timeout=30, http2=True, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        return response.status_code, response.text


async def _solve_deepseek_pow(challenge: dict[str, Any]) -> str:
    node = shutil.which("node")
    if not node:
        raise ProviderError("DeepSeek Web 的 PoW 求解需要 Node.js 18+", "invalid_config", 500)
    script = Path(__file__).with_name("deepseek_pow.js")
    creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    process = await asyncio.create_subprocess_exec(
        node, str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(challenge, separators=(",", ":")).encode()),
            timeout=20,
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ProviderError("DeepSeek Web 的 PoW 求解超时", "challenge") from exc
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:240]
        raise ProviderError(f"DeepSeek Web 的 PoW 求解失败{': ' + detail if detail else ''}", "challenge")
    value = stdout.decode().strip()
    if not value:
        raise ProviderError("DeepSeek Web 的 PoW 求解结果为空", "challenge")
    return value


def _deepseek_biz_data(raw: str, action: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
        if payload.get("code") not in {None, 0}:
            code = payload.get("code")
            status = "auth_expired" if code in {40002, 40003} else "upstream_error"
            raise ProviderError(f"DeepSeek {action}失败：{payload.get('msg') or code}", status)
        data = payload.get("data", {}).get("biz_data", {})
        if payload.get("data", {}).get("biz_code") not in {None, 0}:
            raise ProviderError(f"DeepSeek {action}失败：{payload.get('data', {}).get('biz_msg') or payload.get('data', {}).get('biz_code')}", "upstream_error")
        return data
    except json.JSONDecodeError as exc:
        raise ProviderError(f"DeepSeek {action}响应格式已变化", "protocol_mismatch") from exc


def _parse_deepseek_stream(raw: str) -> list[CanonicalEvent]:
    events: list[CanonicalEvent] = []
    active_path = ""
    active_operation = ""
    fragment_type = "RESPONSE"
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if data.get("p") is not None:
            active_path = str(data["p"])
        if data.get("o") is not None:
            active_operation = str(data["o"])
        value = data.get("v")
        if isinstance(value, dict):
            response = value.get("response") or {}
            for fragment in response.get("fragments") or []:
                content = fragment.get("content")
                if not isinstance(content, str) or not content:
                    continue
                fragment_type = str(fragment.get("type") or "RESPONSE")
                if fragment_type in {"THINK", "THINKING", "REASONING"}:
                    events.append(CanonicalEvent("reasoning", reasoning=content))
                else:
                    events.append(CanonicalEvent("text", text=content))
        elif isinstance(value, str) and active_operation == "APPEND" and active_path.endswith("/content"):
            if fragment_type in {"THINK", "THINKING", "REASONING"}:
                events.append(CanonicalEvent("reasoning", reasoning=value))
            else:
                events.append(CanonicalEvent("text", text=value))
    return events


def _deepseek_stream_hint(raw: str) -> str:
    """Describe an unrecognised response without persisting prompts or answer text."""
    if not raw.strip():
        return "HTTP 200 空响应"
    markers: list[str] = []
    for line in raw.splitlines()[:80]:
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        for key in ("code", "biz_code", "status", "msg", "biz_msg", "error"):
            value = data.get(key)
            if value not in (None, "", 0) and not isinstance(value, (dict, list)):
                markers.append(f"{key}={re.sub(r'\s+', ' ', str(value))[:80]}")
        if data.get("p") is not None or data.get("o") is not None:
            markers.append(f"event={data.get('o', '?')}:{data.get('p', '?')}")
        if len(markers) >= 4:
            break
    if markers:
        return "HTTP 200，" + "，".join(dict.fromkeys(markers))
    return f"HTTP 200，未识别事件结构（{len(raw.encode(errors='replace'))} bytes）"


def _jwt_expiry(token: str) -> int | None:
    try:
        raw = token.removeprefix("Bearer ").removeprefix("bearer ").split(".")[1]
        raw += "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
        return int(payload["exp"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def refresh_kimi_credential(source: dict[str, Any]) -> bool:
    """在访问令牌即将到期时使用扩展捕获的刷新令牌续期。"""
    if source.get("protocol") != "kimi_web":
        return False
    credential = source.get("credential") or {}
    refresh_token = str(credential.get("refresh_token") or "").strip()
    if not refresh_token:
        return False
    headers = {str(key): str(value) for key, value in (credential.get("headers") or {}).items()}
    auth_name = next((key for key in headers if key.lower() == "authorization"), "Authorization")
    access_token = headers.get(auth_name, "")
    expiry = _jwt_expiry(access_token)
    if expiry is not None and expiry > int(time.time()) + 90:
        return False

    captured_url = str(credential.get("request_url") or "https://www.kimi.com")
    parts = urlsplit(captured_url)
    origin = f"{parts.scheme or 'https'}://{parts.netloc or 'www.kimi.com'}"
    refresh_headers = {
        key: value for key, value in headers.items()
        if key.lower().startswith("x-msh-") or key.lower() in {"accept-language", "r-timezone", "user-agent"}
    }
    refresh_headers.update({
        "content-type": "application/json",
        "connect-protocol-version": "1",
        "origin": origin,
        "referer": f"{origin}/",
    })
    timeout = httpx.Timeout(15, read=30, write=15, pool=10)
    try:
        async with httpx.AsyncClient(timeout=timeout, http2=True, follow_redirects=True) as client:
            response = await client.post(
                "https://auth.kimi.com/api/account.gateway.v1.AuthService/RefreshToken",
                headers=refresh_headers,
                json={"refresh_token": refresh_token},
            )
    except httpx.HTTPError as exc:
        raise ProviderError(f"Kimi 访问令牌刷新失败：{str(exc)[:180]}", "network_error") from exc
    if response.status_code >= 400:
        raise classify_http(response.status_code, response.text)
    try:
        data = response.json()
        access = data.get("access_token") or data.get("accessToken")
        refreshed = data.get("refresh_token") or data.get("refreshToken")
    except (AttributeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("Kimi 刷新令牌响应格式已变化", "protocol_mismatch") from exc
    if not isinstance(access, str) or not access or not isinstance(refreshed, str) or not refreshed:
        raise ProviderError("Kimi 刷新令牌响应缺少新令牌", "protocol_mismatch")
    headers[auth_name] = access if access.lower().startswith("bearer ") else f"Bearer {access}"
    credential["headers"] = headers
    credential["refresh_token"] = refreshed
    source["credential"] = credential
    return True


async def _stream_qwen(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    captured_url, headers = _web_headers(source)
    if not captured_url and not headers.get("cookie"):
        raise ProviderError("千问尚未配置 Cookie/cURL", "unconfigured", 400)
    base = source["base_url"].rstrip("/")
    completion_url = captured_url or f"{base}/api/v2/chat/completions"
    parts = urlsplit(completion_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    timestamp = int(time.time())
    if not any(key.lower() == "version" for key in headers):
        headers["Version"] = "0.2.91"

    def request_headers() -> dict[str, str]:
        current = {key: value for key, value in headers.items() if key.lower() != "x-request-id"}
        current["X-Request-Id"] = str(uuid.uuid4())
        return current

    chat_seed = {"chatId": "", "models": [req.upstream_model], "project_id": "", "timestamp": timestamp * 1000, "chat_type": "t2t", "chat_mode": "normal"}
    status, raw = await _curl_post(f"{parts.scheme}://{parts.netloc}/api/v2/chats/new", request_headers(), chat_seed)
    if status >= 400:
        raise classify_http(status, raw)
    try:
        chat_id = json.loads(raw)["data"]["id"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProviderError("千问新建会话响应格式已变化", "protocol_mismatch") from exc
    query["chat_id"] = chat_id
    completion_url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/api/v2/chat/completions", urlencode(query), ""))
    fid = str(uuid.uuid4())
    body = {
        "stream": True, "version": "2.1", "incremental_output": True, "chatId": chat_id, "parentId": "", "chat_id": chat_id,
        "chat_mode": "normal", "model": req.upstream_model, "parent_id": None,
        "messages": [{"id": None, "fid": fid, "parentId": None, "childrenIds": [str(uuid.uuid4())], "role": "user", "content": flatten_web_prompt(req), "user_action": "chat", "files": [], "timestamp": timestamp, "models": [req.upstream_model], "model": "", "chat_type": "t2t", "feature_config": {"thinking_enabled": True, "output_schema": "phase", "research_mode": "normal", "auto_thinking": True, "thinking_mode": "Auto", "thinking_format": "summary", "auto_search": False}, "extra": {"meta": {"subChatType": "t2t"}}, "sub_chat_type": "t2t", "parent_id": None}],
        "timestamp": timestamp,
    }
    status, raw = await _curl_post(completion_url, request_headers(), body)
    if status >= 400:
        raise classify_http(status, raw)
    saw_event = False
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if data.get("usage"):
            yield CanonicalEvent("usage", usage=data["usage"])
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content") or ""
            if content:
                saw_event = True
                if delta.get("phase") == "answer":
                    yield CanonicalEvent("text", text=content)
                else:
                    yield CanonicalEvent("reasoning", reasoning=content)
    if not saw_event:
        raise ProviderError("千问响应中没有可识别的回答，Cookie/风控请求头可能已过期", "protocol_mismatch")


async def _stream_doubao(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    request_url, headers = _web_headers(source)
    if not request_url or "a_bogus=" not in request_url or "msToken=" not in request_url:
        raise ProviderError("豆包需要从 F12 粘贴包含 a_bogus 与 msToken 的完整 cURL", "invalid_config", 400)
    now_ms = int(time.time() * 1000)
    local_id = f"local_{now_ms}{uuid.uuid4().int % 10000:04d}"
    body = {
        "client_meta": {"local_conversation_id": local_id, "conversation_id": "", "bot_id": "7338286299411103781", "last_section_id": "", "last_message_index": None, "local_permissions": []},
        "messages": [{"local_message_id": str(uuid.uuid4()), "content_block": [{"block_type": 10000, "content": {"text_block": {"text": flatten_web_prompt(req), "icon_url": "", "icon_url_dark": "", "summary": ""}, "pc_event_block": ""}, "block_id": str(uuid.uuid4()), "parent_id": "", "meta_info": [], "append_fields": []}], "message_status": 0}],
        "option": {"send_message_scene": "", "create_time_ms": now_ms, "collect_id": "", "is_audio": False, "answer_with_suggest": False, "agent_mode": 2, "tts_switch": False, "need_deep_think": 0, "click_clear_context": False, "from_suggest": False, "is_regen": False, "is_replace": False, "disable_sse_cache": False, "scene_type": 0, "unique_key": str(uuid.uuid4()), "start_seq": 0, "need_create_conversation": True, "conversation_init_option": {"need_ack_conversation": True}, "conversation_init_ext": {"model_item_key": req.upstream_model or "0", "reasoning_effort": "3", "mode_id": "1"}, "regen_query_id": [], "edit_query_id": [], "sse_recv_event_options": {"support_chunk_delta": True}, "support_lazy_fetch_stream": True, "is_old_user": True, "recovery_option": {"is_recovery": False, "req_create_time_sec": now_ms // 1000, "append_sse_event_scene": 0}, "message_storage_type": 0, "related_deleted_message_ids": {}, "connector_info_list": [], "model_config": {"model_item_key": req.upstream_model or "0", "model_extra_params": {}, "reasoning_effort": 3}, "aggregate_params": {"mention_skill_list": "[]", "mention_plugin_list": "[]", "mention_ext": "[{}]", "conversation_mode": "1", "mode_id": "1", "model_item_key": req.upstream_model or "0", "agent_mode": "2", "reasoning_effort": "3", "provider_id": ""}, "conversation_mode": 1},
        "user_context": [], "ext": {"agent_mode": "2", "use_deep_think": "0", "sub_conv_firstmet_type": "1", "collection_id": "", "is_finish": "1", "conversation_init_option": "{\"need_ack_conversation\":true}", "commerce_credit_config_enable": "0"},
    }
    status, raw = await _curl_post(request_url, headers, body)
    if status >= 400:
        raise classify_http(status, raw)
    event_name = ""
    saw_text = False
    for line in raw.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            try:
                data = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            chunks: list[str] = []
            if event_name == "STREAM_MSG_NOTIFY":
                for block in data.get("content", {}).get("content_block") or []:
                    text = block.get("content", {}).get("text_block", {}).get("text")
                    if text:
                        chunks.append(text)
            elif event_name == "CHUNK_DELTA" and data.get("text"):
                chunks.append(data["text"])
            for chunk in chunks:
                saw_text = True
                yield CanonicalEvent("text", text=chunk)
    if not saw_text:
        raise ProviderError("豆包响应中没有 CHUNK_DELTA，签名/Cookie 可能已过期或协议已变化", "protocol_mismatch")


async def _stream_deepseek(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    captured_url, headers = _web_headers(source)
    if not captured_url or not any(key.lower() == "authorization" for key in headers):
        raise ProviderError("DeepSeek Web 需要包含 Authorization 的完整对话请求 cURL；HAR 通常会删除这个敏感字段", "invalid_config", 400)
    parts = urlsplit(captured_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    completion_url = f"{origin}/api/v0/chat/completion"
    common_headers = {key: value for key, value in headers.items() if key.lower() != "x-ds-pow-response"}
    setup_headers = {key: value for key, value in common_headers.items() if key.lower() not in {"x-hif-leim", "x-hif-dliq"}}

    status, raw = await _curl_post(f"{origin}/api/v0/chat_session/create", setup_headers, {})
    if status >= 400:
        raise classify_http(status, raw)
    session_data = _deepseek_biz_data(raw, "新建会话")
    session_id = session_data.get("chat_session", {}).get("id")
    if not session_id:
        raise ProviderError("DeepSeek 新建会话响应中没有会话 ID", "protocol_mismatch")

    status, raw = await _curl_post(
        f"{origin}/api/v0/chat/create_pow_challenge",
        setup_headers,
        {"target_path": "/api/v0/chat/completion"},
    )
    if status >= 400:
        raise classify_http(status, raw)
    challenge = _deepseek_biz_data(raw, "获取 PoW 挑战").get("challenge")
    if not challenge:
        raise ProviderError("DeepSeek PoW 响应中没有 challenge", "protocol_mismatch")
    common_headers["x-ds-pow-response"] = await _solve_deepseek_pow(challenge)
    try:
        hif_status, hif_raw = await _curl_get(
            "https://hif-leim.deepseek.com/query",
            {key: value for key, value in common_headers.items() if key.lower() in {"accept", "accept-language", "user-agent"}},
        )
        if hif_status < 400:
            hif_value = _deepseek_biz_data(hif_raw, "获取 HIF 签名").get("value")
            if hif_value:
                common_headers["x-hif-leim"] = hif_value
    except (ProviderError, httpx.HTTPError, OSError):
        pass

    body = {
        "chat_session_id": session_id,
        "parent_message_id": None,
        "model_type": req.upstream_model or "default",
        "prompt": flatten_web_prompt(req),
        "ref_file_ids": [],
        "thinking_enabled": False,
        "search_enabled": False,
        "action": None,
        "preempt": False,
    }
    status, raw = await _curl_post(completion_url, common_headers, body)
    if status >= 400:
        raise classify_http(status, raw)

    events = _parse_deepseek_stream(raw)
    if not events:
        raise ProviderError(f"DeepSeek Web 响应中没有可识别的回答：{_deepseek_stream_hint(raw)}", "protocol_mismatch")
    for event in events:
        yield event


async def _stream_kimi(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    captured_url, captured_headers = _web_headers(source)
    has_authorization = any(key.lower() == "authorization" for key in captured_headers)
    if not captured_url or not has_authorization:
        raise ProviderError("Kimi Web 需要包含 Authorization 的完整对话请求 cURL；HAR 通常会删除这个敏感字段", "invalid_config", 400)

    parts = urlsplit(captured_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    chat_url = f"{origin}/apiv2/kimi.gateway.chat.v1.ChatService/Chat"
    replaced_headers = {"content-type", "accept", "connect-protocol-version", "origin", "referer", "cookie"}
    headers = {
        key: value for key, value in captured_headers.items()
        if not key.lower().startswith("x-msh-")
        and key.lower() != "x-traffic-id"
        and key.lower() not in replaced_headers
    }
    headers.update({
        "content-type": "application/connect+json",
        "accept": "*/*",
        "connect-protocol-version": "1",
        "origin": origin,
        "referer": f"{origin}/",
    })
    body = {
        "chat_id": "",
        "scenario": "SCENARIO_CHAT",
        "tools": [],
        "message": {
            "id": "",
            "parent_id": "",
            "children_message_ids": [],
            "role": "user",
            "blocks": [{"id": "", "message_id": "", "text": {"content": flatten_web_prompt(req)}}],
            "scenario": "SCENARIO_CHAT",
            "labels": [],
            "references": [],
            "is_goal": False,
        },
        "options": {
            "thinking": True,
            "enable_plugin": False,
            "reasoning_effort": "REASONING_EFFORT_NONE",
            "model": req.upstream_model or "k2d6-chat",
        },
        "project_id": "",
    }

    timeout = httpx.Timeout(20, read=180, write=30, pool=10)
    buffer = bytearray()
    saw_content = False
    saw_end = False
    async with httpx.AsyncClient(timeout=timeout, http2=True, follow_redirects=True) as client:
        async with client.stream("POST", chat_url, headers=headers, content=_connect_message(body)) as response:
            if response.status_code >= 400:
                raise classify_http(response.status_code, (await response.aread()).decode(errors="replace"))
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                while True:
                    frame = _pop_connect_frame(buffer)
                    if frame is None:
                        break
                    flags, message = frame
                    if flags & 0x02:
                        error = (message or {}).get("error")
                        if isinstance(error, dict):
                            detail = f"{error.get('code', 'unknown')}: {error.get('message', 'upstream error')}"
                            raise ProviderError(f"Kimi Connect 结束帧返回错误：{detail}", "upstream_error")
                        saw_end = True
                        continue
                    if not message:
                        continue
                    event = _kimi_delta(message)
                    if event:
                        saw_content = True
                        yield event
    if buffer:
        raise ProviderError("Kimi Connect 响应在帧中途结束", "protocol_mismatch")
    if not saw_end:
        raise ProviderError("Kimi Connect 响应缺少结束帧", "protocol_mismatch")
    if not saw_content:
        raise ProviderError("Kimi 响应中没有可识别的回答", "protocol_mismatch")


async def _stream_perplexity(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    captured_url, captured_headers = _web_headers(source)
    has_account = any(key.lower() == "x-pplx-account" for key in captured_headers)
    if not captured_url or not has_account:
        raise ProviderError("Perplexity Web 需要包含 x-pplx-account 的完整对话请求 cURL", "invalid_config", 400)

    parts = urlsplit(captured_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    request_url = f"{origin}/rest/sse/perplexity_ask"
    replaced_headers = {"content-type", "accept", "x-request-id"}
    headers = {key: value for key, value in captured_headers.items() if key.lower() not in replaced_headers}
    headers.update({
        "content-type": "application/json",
        "accept": "text/event-stream",
        "origin": origin,
        "referer": f"{origin}/",
        "x-request-id": str(uuid.uuid4()),
    })
    frontend_id = str(uuid.uuid4())
    context_id = str(uuid.uuid4())
    prompt = flatten_web_prompt(req)
    body = {
        "params": {
            "attachments": [],
            "language": "zh-CN",
            "timezone": "Asia/Shanghai",
            "search_focus": "internet",
            "sources": ["web"],
            "frontend_uuid": frontend_id,
            "mode": "copilot",
            "model_preference": req.upstream_model or "turbo",
            "is_related_query": False,
            "frontend_context_uuid": context_id,
            "prompt_source": "user",
            "query_source": "home",
            "is_incognito": False,
            "use_schematized_api": True,
            "send_back_text_in_streaming_api": False,
            "dsl_query": prompt,
            "skip_search_enabled": True,
            "source": "default",
            "always_search_override": False,
            "override_no_search": False,
            "client_search_results_cache_key": frontend_id,
            "rum_session_id": str(uuid.uuid4()),
        },
        "query_str": prompt,
    }
    status, raw = await _curl_post(request_url, headers, body)
    if status >= 400:
        raise classify_http(status, raw)
    chunks = _parse_perplexity_stream(raw)
    if not chunks:
        raise ProviderError("Perplexity 响应中没有可识别的回答", "protocol_mismatch")
    full_text = "".join(chunks)
    if re.search(r"sign up.+repeat your request", full_text, re.IGNORECASE | re.DOTALL):
        raise ProviderError("Perplexity 要求登录，请重新复制登录状态下的完整 cURL", "auth_expired", 401)
    if re.search(r"wait.+repeat your request", full_text, re.IGNORECASE | re.DOTALL):
        raise ProviderError("Perplexity 暂时限流，请稍后重试", "rate_limited", 429)
    for chunk in chunks:
        yield CanonicalEvent("text", text=chunk)


def _wenxin_token(captured: str, prompt: str) -> str:
    try:
        encoded, lid, version = captured.rsplit("-", 2)
        padding = "=" * (-len(encoded) % 4)
        seed, _, _, embedded_lid = base64.b64decode(encoded + padding).decode().split("|", 3)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError("文心 cURL 中的 chat_token 格式无法识别，请重新复制最新请求", "invalid_config", 400) from exc
    if embedded_lid != lid or version != "3":
        raise ProviderError("文心 cURL 中的 chat_token 版本已变化", "protocol_mismatch")
    now_ms = int(time.time() * 1000) + uuid.uuid4().int % 7
    digest = hashlib.md5(prompt.encode()).hexdigest()
    payload = base64.b64encode(f"{seed}|{digest}|{now_ms}|{lid}".encode()).decode()
    return f"{payload}-{lid}-{version}"


def _wenxin_header(fields: list[tuple[str, Any]]) -> str:
    values: list[str] = []
    safe_chars = "-_.!~*'()"
    for name, value in fields:
        if value in (None, "", False):
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        values.append(f"{name}:{quote(str(value), safe=safe_chars)}")
    return ",".join(values)


def _parse_wenxin_stream(raw: str) -> tuple[list[str], int | None, bool]:
    chunks: list[str] = []
    error_status: int | None = None
    logged_in = True
    event_name = ""
    for line in raw.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if event_name != "message" or not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        status = event.get("status")
        if isinstance(status, int) and status:
            error_status = status
        user = event.get("user") or {}
        if user.get("isUserLogin") is False:
            logged_in = False
        message = (event.get("data") or {}).get("message") or {}
        user_status = ((message.get("metaData") or {}).get("userInfo") or {}).get("status")
        if user_status == -1:
            logged_in = False
        generator = ((message.get("content") or {}).get("generator") or {})
        data = generator.get("data") or {}
        value = data.get("value") if isinstance(data, dict) else None
        if generator.get("component") == "markdown-yiyan" and isinstance(value, str) and value:
            chunks.append(value)
    return chunks, error_status, logged_in


async def _stream_wenxin(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    captured_url, captured_headers = _web_headers(source)
    captured_body = str((source.get("credential") or {}).get("body") or "")
    if not captured_url or not captured_body:
        raise ProviderError("文心 Web 需要包含请求正文与 Cookie 的完整对话请求 cURL；HAR 会删除 Cookie", "invalid_config", 400)
    if not any(key.lower() == "cookie" for key in captured_headers):
        raise ProviderError("文心 Web 的 cURL 中没有 Cookie；请使用 Network 里的 Copy as cURL (bash)，不要使用 HAR", "invalid_config", 400)
    try:
        body = json.loads(captured_body)
        message = body["message"]
        search_info = message["searchInfo"]
        chat_params = search_info["chatParams"]
        captured_token = str(chat_params["chat_token"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProviderError("文心 cURL 的 JSON 请求正文不完整，请重新复制 conversation 请求", "invalid_config", 400) from exc

    prompt = flatten_web_prompt(req)
    query = message.get("query")
    if not isinstance(query, list) or not query:
        raise ProviderError("文心请求正文缺少 query 模板", "protocol_mismatch")
    text_data = (query[0].get("data") or {}).get("text")
    if isinstance(text_data, dict):
        text_data["query"] = prompt
    elif isinstance(text_data, str):
        query[0]["data"]["text"] = prompt
    else:
        raise ProviderError("文心请求正文的 query 结构已变化", "protocol_mismatch")

    anti_ext = message.get("anti_ext") if isinstance(message.get("anti_ext"), dict) else {}
    anti_ext.update({"inputT": None, "ck1": uuid.uuid4().int % 120 + 30, "ck9": uuid.uuid4().int % 640 + 120, "ck10": uuid.uuid4().int % 360 + 80})
    message["anti_ext"] = anti_ext
    chat_params["chat_token"] = _wenxin_token(captured_token, prompt)
    headers = {key: value for key, value in captured_headers.items() if key.lower() not in {"x-chat-message", "content-type", "accept"}}
    used_model = search_info.get("usedModel") or {}
    headers.update({
        "content-type": "application/json",
        "accept": "text/event-stream",
        "x-chat-message": _wenxin_header([
            ("query", prompt[:500]),
            ("anti_ext", anti_ext),
            ("enter_type", search_info.get("enter_type")),
            ("re_rank", search_info.get("re_rank")),
            ("modelName", used_model.get("modelName")),
            ("sa", search_info.get("sa")),
        ]),
    })
    parts = urlsplit(captured_url)
    request_url = f"{parts.scheme}://{parts.netloc}/aichat/api/conversation"
    status, raw = await _curl_post(request_url, headers, body)
    if status >= 400:
        raise classify_http(status, raw)
    chunks, event_status, logged_in = _parse_wenxin_stream(raw)
    if not chunks:
        if not logged_in:
            raise ProviderError("文心登录状态已失效，请重新复制完整 cURL", "auth_expired", 401)
        suffix = f"（status={event_status}）" if event_status is not None else ""
        raise ProviderError(f"文心响应中没有可识别的回答{suffix}，Cookie/风控字段可能已过期", "protocol_mismatch")
    for chunk in chunks:
        yield CanonicalEvent("text", text=chunk)


async def _bridge_web_tools(
    events: AsyncIterator[CanonicalEvent],
    req: CanonicalRequest,
    dialect: str = "standard",
) -> AsyncIterator[CanonicalEvent]:
    """网页端只会返回文本；缓冲答案并还原成标准工具事件。"""
    text_parts: list[str] = []
    async for event in events:
        if event.type == "text":
            text_parts.append(event.text)
        else:
            yield event
    visible, tool_calls = parse_web_tool_response("".join(text_parts), req.tools, dialect)
    if visible:
        yield CanonicalEvent("text", text=visible)
    for tool in tool_calls:
        yield CanonicalEvent("tool", tool=tool)


def stream_source(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    protocol = source["protocol"]
    if protocol == "openai":
        return _stream_openai(source, req)
    if protocol == "anthropic":
        return _stream_anthropic(source, req)
    if protocol == "qwen_web":
        events = _stream_qwen(source, req)
        dialect = "standard"
    elif protocol == "doubao_web":
        events = _stream_doubao(source, req)
        dialect = "standard"
    elif protocol == "deepseek_web":
        events = _stream_deepseek(source, req)
        dialect = "deepseek"
    elif protocol == "kimi_web":
        events = _stream_kimi(source, req)
        dialect = "standard"
    elif protocol == "perplexity_web":
        events = _stream_perplexity(source, req)
        dialect = "standard"
    elif protocol == "wenxin_web":
        events = _stream_wenxin(source, req)
        dialect = "standard"
    elif protocol == "longcat_web":
        events = _stream_longcat(source, req)
        dialect = "standard"
    else:
        raise ProviderError("该 Web 来源尚未实现直连适配器", "unsupported", 400)
    return _bridge_web_tools(events, req, dialect) if web_tool_bridge_enabled(req) else events


async def health_check(source: dict[str, Any], upstream_model: str) -> tuple[str, str]:
    if source["protocol"] == "unsupported_web":
        return "unsupported", "适配器尚未接入"
    credential = source.get("credential") or {}
    if not credential or (source["kind"] == "api" and not credential.get("api_key")):
        return "unconfigured", "尚未配置凭据"
    req = CanonicalRequest(model="health-check", upstream_model=upstream_model, messages=[{"role": "user", "content": "只回复 OK"}], max_tokens=64)
    try:
        text = ""
        reasoning = ""
        async for event in stream_source(source, req):
            if event.type == "text":
                text += event.text
            elif event.type == "reasoning":
                reasoning += event.reasoning
        if text.strip():
            return "healthy", f"可用，测试回复：{text.strip()[:40]}"
        if reasoning.strip():
            return "healthy", "可用，已收到上游推理流"
        return "protocol_mismatch", "上游连通，但未解析到文本回复"
    except ProviderError as exc:
        return exc.status, str(exc)
    except (httpx.TimeoutException, TimeoutError):
        return "network_error", "连接上游超时"
    except (httpx.HTTPError, OSError) as exc:
        return "network_error", f"网络错误：{str(exc)[:220]}"
    except Exception as exc:
        return "upstream_error", f"健康检查失败：{type(exc).__name__}: {str(exc)[:200]}"
