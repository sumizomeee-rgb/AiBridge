from __future__ import annotations

import ipaddress
import json
import socket
import time
import uuid
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .protocols import (
    anthropic_response,
    anthropic_sse,
    collect_events,
    from_anthropic,
    from_openai,
    openai_response,
    openai_sse,
    sse,
)
from .providers import ProviderError, _api_headers, endpoint, health_check, parse_curl, stream_source
from .settings import STATIC_DIR, settings
from .storage import Storage


storage = Storage(settings.database_path, settings.secret_key_path)


def _lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _private_client(host: str | None) -> bool:
    if not host or host in {"testclient", "localhost"}:
        return True
    try:
        address = ipaddress.ip_address(host.removeprefix("::ffff:"))
        return address.is_private or address.is_loopback or address.is_link_local
    except ValueError:
        return False


def _openai_error(message: str, status: int = 400, code: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": code, "param": None, "code": code}}, status_code=status)


def _anthropic_error(message: str, status: int = 400, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)


def create_gateway_app() -> FastAPI:
    app = FastAPI(title="AiBridge Gateway", version="2.0.0", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def access_guard(request: Request, call_next: Callable):
        if not _private_client(request.client.host if request.client else None):
            return JSONResponse({"error": {"message": "AiBridge 仅允许本机和私有局域网访问"}}, status_code=403)
        if request.url.path != "/health" and not storage.verify_key(_token(request)):
            is_anthropic = request.url.path.endswith("/messages") or "/messages/" in request.url.path
            return _anthropic_error("无效或缺失的网关 Token", 401, "authentication_error") if is_anthropic else _openai_error("无效或缺失的网关 Token", 401, "authentication_error")
        return await call_next(request)

    @app.get("/health")
    async def gateway_health():
        return {"ok": True, "service": "AiBridge Gateway", "port": settings.gateway_port}

    @app.get("/v1/models")
    @app.get("/models")
    async def models():
        created = int(time.time())
        return {"object": "list", "data": [{"id": x["public_name"], "object": "model", "created": created, "owned_by": "aibridge", "source": x["source_name"]} for x in storage.list_models()]}

    async def _prepare(request: Request, protocol: str):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise ProviderError("请求体不是有效 JSON", "invalid_request", 400)
        public_model = body.get("model", "")
        resolved = storage.resolve_model(public_model)
        if not resolved:
            raise ProviderError(f"找不到或未启用模型：{public_model}", "model_not_found", 404)
        source, model = resolved
        canonical = from_openai(body, model["upstream_name"]) if protocol == "openai" else from_anthropic(body, model["upstream_name"])
        return body, public_model, source, canonical

    async def _non_stream(request: Request, protocol: str):
        started = time.perf_counter()
        request_id = ("chatcmpl-" if protocol == "openai" else "msg_") + uuid.uuid4().hex
        source: dict[str, Any] | None = None
        public_model = ""
        try:
            _, public_model, source, canonical = await _prepare(request, protocol)
            text, usage, tools = await collect_events(stream_source(source, canonical))
            storage.update_health(source["id"], "healthy", "最近一次网关请求成功")
            storage.add_log(request_id, source["name"], public_model, protocol, "ok", int((time.perf_counter() - started) * 1000))
            return openai_response(request_id, public_model, text, usage, tools) if protocol == "openai" else anthropic_response(request_id, public_model, text, usage, tools)
        except ProviderError as exc:
            storage.add_log(request_id, source["name"] if source else "-", public_model, protocol, "error", int((time.perf_counter() - started) * 1000), str(exc))
            return _openai_error(str(exc), exc.http_status, exc.status) if protocol == "openai" else _anthropic_error(str(exc), exc.http_status, exc.status)
        except Exception as exc:
            storage.add_log(request_id, source["name"] if source else "-", public_model, protocol, "error", int((time.perf_counter() - started) * 1000), str(exc))
            return _openai_error(f"网关内部错误：{type(exc).__name__}", 500, "internal_error") if protocol == "openai" else _anthropic_error(f"网关内部错误：{type(exc).__name__}", 500, "api_error")

    async def _stream(request: Request, protocol: str):
        started = time.perf_counter()
        request_id = ("chatcmpl-" if protocol == "openai" else "msg_") + uuid.uuid4().hex
        try:
            _, public_model, source, canonical = await _prepare(request, protocol)
        except ProviderError as exc:
            return _openai_error(str(exc), exc.http_status, exc.status) if protocol == "openai" else _anthropic_error(str(exc), exc.http_status, exc.status)

        async def output() -> AsyncIterator[bytes]:
            status = "ok"
            error = None
            try:
                translated = openai_sse(stream_source(source, canonical), request_id, public_model) if protocol == "openai" else anthropic_sse(stream_source(source, canonical), request_id, public_model)
                async for chunk in translated:
                    yield chunk
            except Exception as exc:
                status, error = "error", str(exc)
                if protocol == "openai":
                    yield sse({"error": {"message": str(exc), "type": getattr(exc, "status", "upstream_error")}})
                    yield b"data: [DONE]\n\n"
                else:
                    yield sse({"type": "error", "error": {"type": getattr(exc, "status", "api_error"), "message": str(exc)}}, "error")
            finally:
                if status == "ok":
                    storage.update_health(source["id"], "healthy", "最近一次网关请求成功")
                storage.add_log(request_id, source["name"], public_model, protocol, status, int((time.perf_counter() - started) * 1000), error)

        return StreamingResponse(output(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def chat_completions(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _openai_error("请求体不是有效 JSON")
        return await (_stream(request, "openai") if body.get("stream", False) else _non_stream(request, "openai"))

    @app.post("/v1/messages")
    @app.post("/messages")
    async def messages(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _anthropic_error("请求体不是有效 JSON")
        return await (_stream(request, "anthropic") if body.get("stream", False) else _non_stream(request, "anthropic"))

    @app.post("/v1/messages/count_tokens")
    @app.post("/messages/count_tokens")
    async def count_tokens(request: Request):
        body = await request.json()
        raw = json.dumps({"system": body.get("system"), "messages": body.get("messages")}, ensure_ascii=False)
        return {"input_tokens": max(1, len(raw) // 4)}

    return app


def create_admin_app() -> FastAPI:
    app = FastAPI(title="AiBridge Admin", version="2.0.0", docs_url=None, redoc_url=None)

    @app.get("/api/state")
    async def state():
        lan = _lan_ip()
        return {
            "gateway": {"local_url": f"http://127.0.0.1:{settings.gateway_port}", "lan_url": f"http://{lan}:{settings.gateway_port}", "admin_url": f"http://127.0.0.1:{settings.admin_port}"},
            "sources": storage.list_sources(), "keys": storage.list_keys(), "logs": storage.list_logs(),
        }

    @app.post("/api/keys")
    async def create_key(request: Request):
        body = await request.json()
        item, token = storage.create_key(body.get("name", "本地 Token"))
        return {"key": item, "token": token, "notice": "Token 仅显示这一次，请立即复制保存。"}

    @app.delete("/api/keys/{key_id}")
    async def delete_key(key_id: str):
        storage.delete_key(key_id)
        return {"ok": True}

    @app.post("/api/tools/parse-curl")
    async def parse_curl_api(request: Request):
        body = await request.json()
        try:
            return parse_curl(body.get("curl", ""))
        except ProviderError as exc:
            raise HTTPException(exc.http_status, str(exc)) from exc

    @app.post("/api/sources")
    async def create_source(request: Request):
        body = await request.json()
        try:
            sid = storage.save_source(body)
            for model in body.get("models") or []:
                storage.save_model(sid, model["public_name"], model.get("upstream_name") or model["public_name"], model.get("enabled", True))
            return {"id": sid}
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, f"配置不完整：{exc}") from exc

    @app.put("/api/sources/{source_id}")
    async def update_source(source_id: str, request: Request):
        old = storage.get_source(source_id)
        if not old:
            raise HTTPException(404, "来源不存在")
        body = await request.json()
        if body.get("curl"):
            parsed = parse_curl(body.pop("curl"))
            if body.get("credential", {}).get("cookie"):
                parsed["cookie"] = body["credential"]["cookie"]
            body["credential"] = parsed
        merged = {**old, **body}
        storage.save_source(merged, source_id)
        model = body.get("model")
        if model:
            existing = old.get("models") or []
            storage.save_model(source_id, model["public_name"], model.get("upstream_name") or "default", model.get("enabled", True), model.get("id") or (existing[0]["id"] if existing else None))
        if "models" in body:
            saved_model_ids = []
            for item in body.get("models") or []:
                saved_model_ids.append(storage.save_model(source_id, item["public_name"], item.get("upstream_name") or item["public_name"], item.get("enabled", True), item.get("id")))
            storage.delete_models_except(source_id, saved_model_ids)
        return {"ok": True}

    @app.delete("/api/sources/{source_id}")
    async def delete_source(source_id: str):
        storage.delete_source(source_id)
        return {"ok": True}

    @app.post("/api/sources/{source_id}/health")
    async def source_health(source_id: str):
        source = storage.get_source(source_id, include_secret=True)
        if not source:
            raise HTTPException(404, "来源不存在")
        models = next((x["models"] for x in storage.list_sources() if x["id"] == source_id), [])
        upstream = models[0]["upstream_name"] if models else "default"
        status, message = await health_check(source, upstream)
        storage.update_health(source_id, status, message)
        return {"status": status, "message": message}

    @app.post("/api/sources/{source_id}/discover-models")
    async def discover_models(source_id: str):
        source = storage.get_source(source_id, include_secret=True)
        if not source or source["kind"] != "api":
            raise HTTPException(400, "仅标准 API 来源支持同步模型")
        base = source["base_url"].rstrip("/")
        if source["protocol"] == "anthropic" and base.endswith("/anthropic"):
            url = base[: -len("/anthropic")] + "/models"
        else:
            url = endpoint(base, "models")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(url, headers=_api_headers(source))
            if response.status_code >= 400:
                raise HTTPException(502, f"上游模型接口返回 HTTP {response.status_code}")
            items = response.json().get("data") or response.json().get("models") or []
            names = [x.get("id") or x.get("name") for x in items if isinstance(x, dict) and (x.get("id") or x.get("name"))]
            return {"models": names}
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, f"模型同步失败：{exc}") from exc

    @app.post("/api/models")
    async def create_model(request: Request):
        body = await request.json()
        try:
            model_id = storage.save_model(body["source_id"], body["public_name"], body.get("upstream_name") or body["public_name"], body.get("enabled", True))
            return {"id": model_id}
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, f"模型配置不完整：{exc}") from exc

    @app.put("/api/models/{model_id}")
    async def update_model(model_id: str, request: Request):
        body = await request.json()
        storage.save_model(body["source_id"], body["public_name"], body.get("upstream_name") or body["public_name"], body.get("enabled", True), model_id)
        return {"ok": True}

    @app.delete("/api/models/{model_id}")
    async def delete_model(model_id: str):
        storage.delete_model(model_id)
        return {"ok": True}

    @app.get("/api/logs")
    async def logs():
        return {"logs": storage.list_logs()}

    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/{path:path}")
    async def admin_spa(path: str):
        return FileResponse(STATIC_DIR / "index.html")

    return app


gateway_app = create_gateway_app()
admin_app = create_admin_app()
