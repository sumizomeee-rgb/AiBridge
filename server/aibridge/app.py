from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
import uuid
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .catcher import CAPTURE_RULES, build_capture_credential, public_capture_rules
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
from .providers import ProviderError, _api_headers, endpoint, health_check, parse_curl, refresh_kimi_credential
from .routing import AUTO_SOURCE_ID, RouteContext, SourceScheduler, WebRouter, batch_health_candidates, source_concurrency
from .relay import relay_broker
from .settings import STATIC_DIR, settings
from .storage import Storage


storage = Storage(settings.database_path, settings.secret_key_path)
source_scheduler = SourceScheduler()
web_router = WebRouter(storage, source_scheduler)


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


def _loopback_client(host: str | None) -> bool:
    if not host or host in {"testclient", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(host.removeprefix("::ffff:")).is_loopback
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
        if source["kind"] == "api" and source["protocol"] != protocol:
            expected = "/v1/messages" if source["protocol"] == "anthropic" else "/v1/chat/completions"
            label = "Anthropic API" if source["protocol"] == "anthropic" else "OpenAI API"
            raise ProviderError(
                f"模型 {public_model} 来自 {label} 条目，请改用 {expected}",
                "protocol_mismatch",
                400,
            )
        canonical = from_openai(body, model["upstream_name"]) if protocol == "openai" else from_anthropic(body, model["upstream_name"])
        return body, public_model, source, canonical

    async def _non_stream(request: Request, protocol: str):
        started = time.perf_counter()
        request_id = ("chatcmpl-" if protocol == "openai" else "msg_") + uuid.uuid4().hex
        source: dict[str, Any] | None = None
        route: RouteContext | None = None
        public_model = ""
        try:
            _, public_model, source, canonical = await _prepare(request, protocol)
            route = RouteContext(source)
            text, usage, tools = await collect_events(web_router.stream(source, canonical, route))
            actual = route.actual_source or source
            if actual.get("kind") == "web":
                storage.update_health(actual["id"], "healthy", "最近一次网关请求成功")
            storage.add_log(request_id, route.source_label, public_model, protocol, "ok", int((time.perf_counter() - started) * 1000), route.note)
            return openai_response(request_id, public_model, text, usage, tools) if protocol == "openai" else anthropic_response(request_id, public_model, text, usage, tools)
        except ProviderError as exc:
            note = " · ".join(item for item in (route.note if route else None, str(exc)) if item)
            storage.add_log(request_id, route.source_label if route else (source["name"] if source else "-"), public_model, protocol, "error", int((time.perf_counter() - started) * 1000), note)
            return _openai_error(str(exc), exc.http_status, exc.status) if protocol == "openai" else _anthropic_error(str(exc), exc.http_status, exc.status)
        except Exception as exc:
            note = " · ".join(item for item in (route.note if route else None, str(exc)) if item)
            storage.add_log(request_id, route.source_label if route else (source["name"] if source else "-"), public_model, protocol, "error", int((time.perf_counter() - started) * 1000), note)
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
            route = RouteContext(source)
            try:
                events = web_router.stream(source, canonical, route)
                translated = openai_sse(events, request_id, public_model) if protocol == "openai" else anthropic_sse(events, request_id, public_model)
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
                    actual = route.actual_source or source
                    if actual.get("kind") == "web":
                        storage.update_health(actual["id"], "healthy", "最近一次网关请求成功")
                detail = " · ".join(item for item in (route.note, error) if item)
                storage.add_log(request_id, route.source_label, public_model, protocol, status, int((time.perf_counter() - started) * 1000), detail)

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
    web_health_batch_lock = asyncio.Lock()
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"chrome-extension://[a-p]{32}",
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )

    def catcher_client(request: Request) -> dict[str, Any]:
        client = storage.verify_catcher_client(_token(request))
        if not client:
            raise HTTPException(401, "扩展尚未配对或配对已撤销")
        return client

    @app.get("/api/state")
    async def state(request: Request):
        lan = _lan_ip()
        sources = web_router.runtime_sources(storage.list_sources())
        catcher = storage.catcher_state()
        catcher["rules"] = public_capture_rules()
        return {
            "gateway": {"local_url": f"http://127.0.0.1:{settings.gateway_port}", "lan_url": f"http://{lan}:{settings.gateway_port}", "admin_url": f"http://127.0.0.1:{settings.admin_port}"},
            "permissions": {"source_toggle": _loopback_client(request.client.host if request.client else None)},
            "sources": sources, "keys": storage.list_keys(), "logs": storage.list_logs(), "catcher": catcher,
        }

    @app.patch("/api/catcher/settings")
    async def catcher_settings(request: Request):
        if not _loopback_client(request.client.host if request.client else None):
            raise HTTPException(403, "浏览器同步仅允许在本机管理")
        body = await request.json()
        if type(body.get("enabled")) is not bool or type(body.get("auto_enable")) is not bool:
            raise HTTPException(400, "enabled 与 auto_enable 必须是布尔值")
        requested = body.get("allowed_source_ids")
        if not isinstance(requested, list):
            raise HTTPException(400, "allowed_source_ids 必须是数组")
        allowed = list(dict.fromkeys(
            source_id for source_id in requested
            if isinstance(source_id, str) and source_id in CAPTURE_RULES
        ))
        storage.update_catcher_settings(
            enabled=body["enabled"],
            auto_enable=body["auto_enable"],
            allowed_source_ids=allowed,
        )
        return {"ok": True}

    @app.post("/api/catcher/pairing")
    async def catcher_pairing(request: Request):
        if not _loopback_client(request.client.host if request.client else None):
            raise HTTPException(403, "配对码仅允许在本机生成")
        current = storage.catcher_state()
        if not current["enabled"]:
            raise HTTPException(409, "请先开启浏览器同步")
        code, expires_at = storage.create_catcher_pairing()
        return {"code": code, "expires_at": expires_at, "notice": "配对码十分钟内有效，使用一次后立即失效。"}

    @app.delete("/api/catcher/clients/{client_id}")
    async def revoke_catcher_client(client_id: str, request: Request):
        if not _loopback_client(request.client.host if request.client else None):
            raise HTTPException(403, "扩展配对仅允许在本机管理")
        storage.revoke_catcher_client(client_id)
        return {"ok": True}

    @app.post("/api/catcher/v1/pair")
    async def pair_catcher_extension(request: Request):
        body = await request.json()
        paired = storage.consume_catcher_pairing(str(body.get("code") or ""), str(body.get("name") or "Chrome 扩展"))
        if not paired:
            raise HTTPException(401, "配对码无效、已过期或浏览器同步未开启")
        client, token = paired
        current = storage.catcher_state()
        return {
            "client": client,
            "token": token,
            "protocol_version": 2,
            "enabled": current["enabled"],
            "allowed_source_ids": current["allowed_source_ids"],
            "rules": public_capture_rules(),
        }

    @app.get("/api/catcher/v1/status")
    async def catcher_extension_status(request: Request):
        client = catcher_client(request)
        current = storage.catcher_state()
        storage.touch_catcher_client(client["id"])
        return {
            "protocol_version": 2,
            "enabled": current["enabled"],
            "allowed_source_ids": current["allowed_source_ids"],
            "rules": public_capture_rules(),
            "relay_source_ids": await relay_broker.source_ids(),
        }

    @app.websocket("/api/catcher/v1/relay")
    async def catcher_relay(websocket: WebSocket):
        await websocket.accept()
        try:
            auth = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            token = str(auth.get("token") or "") if isinstance(auth, dict) else ""
            client = storage.verify_catcher_client(token)
            current = storage.catcher_state()
            requested = auth.get("source_ids") if isinstance(auth, dict) else []
            source_ids = {
                source_id for source_id in requested
                if isinstance(source_id, str)
                and source_id in current["allowed_source_ids"]
                and source_id == "web-longcat"
            }
            if not client or not current["enabled"]:
                await websocket.close(code=4003, reason="扩展未配对或浏览器同步未开启")
                return
            storage.touch_catcher_client(client["id"])
            await relay_broker.attach(client["id"], websocket, source_ids)
        except (WebSocketDisconnect, RuntimeError):
            return
        except TimeoutError:
            await websocket.close(code=4008, reason="中继认证超时")

    @app.post("/api/catcher/v1/captures")
    async def receive_catcher_capture(request: Request):
        content_length = int(request.headers.get("content-length") or 0)
        if content_length > 5 * 1024 * 1024:
            raise HTTPException(413, "捕获内容过大")
        client = catcher_client(request)
        current = storage.catcher_state()
        if not current["enabled"]:
            raise HTTPException(503, "浏览器同步当前已关闭")
        body = await request.json()
        try:
            source_id, credential = build_capture_credential(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if source_id not in current["allowed_source_ids"]:
            raise HTTPException(403, "该来源未开启自动同步")
        source = storage.get_source(source_id, include_secret=True)
        if not source:
            raise HTTPException(404, "对应 Web 来源不存在")

        merged_credential = source.get("credential", {}) | {
            key: value for key, value in credential.items() if value not in (None, "", {})
        }
        candidate = source | {"credential": merged_credential}
        models = next((item["models"] for item in storage.list_sources() if item["id"] == source_id), [])
        upstream_model = models[0]["upstream_name"] if models else "default"
        status, message = await health_check(candidate, upstream_model)
        storage.touch_catcher_client(client["id"])
        if status != "healthy":
            storage.add_catcher_event(source_id, client["id"], status, message)
            raise HTTPException(422, f"已捕获但健康检查未通过：{message}")

        config = source.get("config", {}) | {
            "captured_by": "AiBridge Catcher",
            "capture_protocol_version": int(body.get("protocol_version") or 1),
        }
        storage.save_source(
            {
                "name": source["name"],
                "kind": source["kind"],
                "protocol": source["protocol"],
                "base_url": source["base_url"],
                "enabled": True if current["auto_enable"] else source["enabled"],
                "config": config,
                "credential": merged_credential,
            },
            source_id,
        )
        storage.update_health(source_id, status, f"浏览器自动同步成功 · {message}")
        storage.add_catcher_event(source_id, client["id"], "healthy", message)
        return {"ok": True, "source_id": source_id, "status": status, "message": message}

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
        body["enabled"] = old["enabled"]
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

    @app.patch("/api/sources/{source_id}/enabled")
    async def toggle_source(source_id: str, request: Request):
        if not _loopback_client(request.client.host if request.client else None):
            raise HTTPException(403, "来源开关仅允许在本机 127.0.0.1 管理台操作")
        source = storage.get_source(source_id)
        if not source:
            raise HTTPException(404, "来源不存在")
        body = await request.json()
        if type(body.get("enabled")) is not bool:
            raise HTTPException(400, "enabled 必须是布尔值")
        storage.set_source_enabled(source_id, body["enabled"])
        return {"ok": True, "enabled": body["enabled"]}

    @app.delete("/api/sources/{source_id}")
    async def delete_source(source_id: str):
        storage.delete_source(source_id)
        return {"ok": True}

    @app.post("/api/sources/{source_id}/health")
    async def source_health(source_id: str):
        source = storage.get_source(source_id, include_secret=True)
        if not source:
            raise HTTPException(404, "来源不存在")
        if source_id == AUTO_SOURCE_ID:
            candidates = web_router.configured_candidates(require_healthy=False)
            if not candidates:
                return {"status": "unconfigured", "message": "没有已启用且已配置的 Web 来源"}
            failures: list[str] = []
            for candidate in candidates:
                lease = await source_scheduler.acquire(candidate["id"], source_concurrency(candidate))
                try:
                    if await refresh_kimi_credential(candidate):
                        storage.update_source_credential(candidate["id"], candidate["credential"])
                    status, message = await health_check(candidate, candidate["route_model"]["upstream_name"])
                finally:
                    await lease.release()
                storage.update_health(candidate["id"], status, message)
                if status == "healthy":
                    return {"status": "healthy", "message": f"当前首选 {candidate['name']}：{message}"}
                failures.append(f"{candidate['name']}：{message}")
            return {"status": "upstream_error", "message": "；".join(failures)[:500]}
        models = next((x["models"] for x in storage.list_sources() if x["id"] == source_id), [])
        upstream = models[0]["upstream_name"] if models else "default"
        lease = None
        if source.get("kind") == "web":
            lease = await source_scheduler.acquire(source["id"], source_concurrency(source))
        try:
            if await refresh_kimi_credential(source):
                storage.update_source_credential(source["id"], source["credential"])
            status, message = await health_check(source, upstream)
        finally:
            if lease:
                await lease.release()
        storage.update_health(source_id, status, message)
        return {"status": status, "message": message}

    @app.post("/api/web-sources/health")
    async def web_sources_health(request: Request):
        body = await request.json()
        force = body.get("force") is True
        if web_health_batch_lock.locked():
            return {"running": True, "checked": 0, "skipped": 0, "results": []}

        async with web_health_batch_lock:
            candidates, skipped = batch_health_candidates(storage.list_sources(), force=force)

            async def check(source: dict[str, Any]) -> dict[str, Any]:
                try:
                    result = await source_health(source["id"])
                except Exception as exc:
                    message = f"批量健康检查失败：{type(exc).__name__}: {str(exc)[:180]}"
                    storage.update_health(source["id"], "upstream_error", message)
                    result = {"status": "upstream_error", "message": message}
                return {"source_id": source["id"], "name": source["name"], **result}

            results = await asyncio.gather(*(check(source) for source in candidates))
            return {
                "running": False,
                "checked": len(results),
                "skipped": skipped,
                "results": results,
            }

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
