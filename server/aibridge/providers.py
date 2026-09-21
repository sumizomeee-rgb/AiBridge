from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except (ImportError, OSError):
    CurlAsyncSession = None

from .protocols import CanonicalEvent, CanonicalRequest, flatten_web_prompt, parse_web_tool_response, to_anthropic_upstream, to_openai_upstream, web_tool_bridge_enabled


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
        i += 1
    for key in list(headers):
        if key.lower() == "cookie":
            cookie = headers.pop(key)
    if not url:
        raise ProviderError("cURL 中没有找到请求 URL", "invalid_config", 400)
    return {"request_url": url, "headers": headers, "cookie": cookie}


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


async def _stream_qwen(source: dict[str, Any], req: CanonicalRequest) -> AsyncIterator[CanonicalEvent]:
    captured_url, headers = _web_headers(source)
    if not captured_url and not headers.get("cookie"):
        raise ProviderError("千问尚未配置 Cookie/cURL", "unconfigured", 400)
    base = source["base_url"].rstrip("/")
    completion_url = captured_url or f"{base}/api/v2/chat/completions"
    parts = urlsplit(completion_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    timestamp = int(time.time())
    chat_seed = {"chatId": "", "models": [req.upstream_model], "project_id": "", "timestamp": timestamp * 1000, "chat_type": "t2t", "chat_mode": "normal"}
    status, raw = await _curl_post(f"{parts.scheme}://{parts.netloc}/api/v2/chats/new", headers, chat_seed)
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
    status, raw = await _curl_post(completion_url, headers, body)
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
        raise ProviderError("DeepSeek Web 响应中没有可识别的回答，登录态或协议可能已变化", "protocol_mismatch")
    for event in events:
        yield event


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
