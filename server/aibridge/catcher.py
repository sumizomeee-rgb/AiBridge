from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


MAX_CAPTURE_BYTES = 4 * 1024 * 1024

CAPTURE_RULES: dict[str, dict[str, Any]] = {
    "web-deepseek": {
        "name": "DeepSeek Web",
        "hosts": {"chat.deepseek.com"},
        "path_contains": "/api/v0/chat/completion",
        "origins": ["https://chat.deepseek.com/*"],
        "grade": "stable",
    },
    "web-kimi": {
        "name": "Kimi Web",
        "hosts": {"www.kimi.com", "kimi.com"},
        "path_contains": "/apiv2/kimi.gateway.chat.v1.ChatService/Chat",
        "origins": ["https://www.kimi.com/*", "https://kimi.com/*"],
        "grade": "stable",
    },
    "web-qwen": {
        "name": "千问 Web",
        "hosts": {"chat.qwen.ai"},
        "path_contains": "/api/v2/chat/completions",
        "origins": ["https://chat.qwen.ai/*"],
        "grade": "beta",
    },
    "web-doubao": {
        "name": "豆包 Web",
        "hosts": {"www.doubao.com", "doubao.com"},
        "path_contains": "/chat/completion",
        "origins": ["https://www.doubao.com/*", "https://doubao.com/*"],
        "grade": "experimental",
    },
    "web-perplexity": {
        "name": "Perplexity Web",
        "hosts": {"www.perplexity.ai", "perplexity.ai"},
        "path_contains": "/rest/sse/perplexity_ask",
        "origins": ["https://www.perplexity.ai/*", "https://perplexity.ai/*"],
        "grade": "stable",
    },
    "web-wenxin": {
        "name": "文心 Web",
        "hosts": {"wenxin.baidu.com", "chat.baidu.com"},
        "path_contains": "/aichat/api/conversation",
        "origins": ["https://wenxin.baidu.com/*", "https://chat.baidu.com/*"],
        "grade": "stable",
    },
    "web-longcat": {
        "name": "LongCat Web",
        "hosts": {"longcat.chat"},
        "path_contains": "/api/v1/chat-completion-V2",
        "origins": ["https://longcat.chat/*"],
        "grade": "experimental",
    },
    "web-mimo": {
        "name": "MiMo Web",
        "hosts": {"aistudio.xiaomimimo.com"},
        "path_contains": "/open-apis/bot/chat",
        "origins": ["https://aistudio.xiaomimimo.com/*"],
        "grade": "experimental",
    },
}


def public_capture_rules() -> list[dict[str, Any]]:
    return [
        {
            "source_id": source_id,
            "name": rule["name"],
            "origins": rule["origins"],
            "grade": rule["grade"],
        }
        for source_id, rule in CAPTURE_RULES.items()
    ]


def validate_capture_target(source_id: str, url: str) -> None:
    rule = CAPTURE_RULES.get(source_id)
    if not rule:
        raise ValueError("该来源不支持浏览器自动同步")
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise ValueError("捕获请求缺少有效 URL")
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in rule["hosts"]:
        raise ValueError("捕获请求与来源域名不匹配")
    if rule["path_contains"] not in parts.path:
        raise ValueError("捕获请求不是该来源的对话接口")


def _header_map(raw: Any) -> dict[str, str]:
    pairs: list[tuple[str, Any]] = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("name"):
                pairs.append((str(item["name"]), item.get("value", "")))

    blocked = {
        "content-length", "host", "accept-encoding", "connection",
        ":authority", ":method", ":path", ":scheme",
    }
    allowed_exact = {
        "accept", "accept-language", "authorization", "content-type", "cookie",
        "connect-protocol-version", "origin", "priority", "referer", "r-timezone",
        "user-agent", "$bx-ua", "bx-umidtoken", "bx-v", "source", "timezone",
        "version",
        "m-appkey", "m-traceid", "mtgsig",
    }
    headers: dict[str, str] = {}
    for name, value in pairs:
        clean_name = name.strip()
        lower = clean_name.lower()
        if not clean_name or lower in blocked:
            continue
        if lower not in allowed_exact and not lower.startswith(("x-", "sec-ch-", "sec-fetch-")):
            continue
        clean_value = str(value)
        if len(clean_name) > 200 or len(clean_value) > 65536:
            continue
        headers[clean_name] = clean_value
    return headers


def _storage_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > 65536:
        return ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, str):
        return parsed.strip()
    if isinstance(parsed, dict):
        for key in ("token", "access_token", "refresh_token", "value"):
            item = parsed.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _captured_body(raw: Any) -> tuple[str, str]:
    if not isinstance(raw, dict):
        return "", ""
    text = raw.get("text")
    if isinstance(text, str):
        encoded = text.encode("utf-8")
        if len(encoded) <= MAX_CAPTURE_BYTES:
            return text, ""
    encoded = raw.get("base64")
    if isinstance(encoded, str) and len(encoded) <= MAX_CAPTURE_BYTES * 2:
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return "", ""
        if len(payload) > MAX_CAPTURE_BYTES:
            return "", ""
        try:
            return payload.decode("utf-8"), ""
        except UnicodeDecodeError:
            return "", encoded
    return "", ""


def build_capture_credential(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    source_id = str(payload.get("source_id") or "")
    request_url = str(payload.get("url") or "")
    validate_capture_target(source_id, request_url)
    if str(payload.get("method") or "POST").upper() != "POST":
        raise ValueError("仅接受对话 POST 请求")

    headers = _header_map(payload.get("headers"))
    cookie = ""
    for name in list(headers):
        if name.lower() == "cookie":
            cookie = headers.pop(name)
            break
    if not cookie and isinstance(payload.get("cookie"), str):
        cookie = payload["cookie"].strip()[:262144]

    body, body_base64 = _captured_body(payload.get("body"))
    if source_id == "web-longcat":
        headers = {
            name: value for name, value in headers.items()
            if name.lower() not in {"mtgsig", "m-traceid"}
        }
        body, body_base64 = "", ""
    elif source_id == "web-mimo":
        parts = urlsplit(request_url)
        ph = (parse_qs(parts.query).get("xiaomichatbot_ph") or [""])[0].strip()
        if not ph or len(ph) > 256:
            raise ValueError("MiMo 捕获请求缺少 xiaomichatbot_ph")
        request_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({"xiaomichatbot_ph": ph}), ""))
        headers = {
            name: value for name, value in headers.items()
            if name.lower() in {
                "accept", "accept-language", "content-type", "origin", "referer",
                "user-agent", "x-timezone",
            } or name.lower().startswith(("sec-ch-", "sec-fetch-"))
        }
        cookie = ""
        body, body_base64 = "", ""
    credential: dict[str, Any] = {
        "request_url": request_url,
        "headers": headers,
        "cookie": cookie,
        "body": body,
    }
    if body_base64:
        credential["body_base64"] = body_base64
    if source_id == "web-longcat":
        credential["browser_relay"] = True
    elif source_id == "web-mimo":
        credential["browser_relay"] = True

    page_storage = payload.get("page_storage")
    if source_id == "web-kimi" and isinstance(page_storage, dict):
        access_token = _storage_token(page_storage.get("access_token"))
        refresh_token = _storage_token(page_storage.get("refresh_token"))
        if access_token:
            existing = next((name for name in headers if name.lower() == "authorization"), "Authorization")
            credential["headers"][existing] = access_token if access_token.lower().startswith("bearer ") else f"Bearer {access_token}"
        if refresh_token:
            credential["refresh_token"] = refresh_token

    return source_id, credential
