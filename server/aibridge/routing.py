from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, AsyncIterator, Callable

from .protocols import CanonicalEvent, CanonicalRequest
from .providers import ProviderError, refresh_kimi_credential, stream_source
from .storage import Storage


AUTO_SOURCE_ID = "web-auto"
AUTO_SOURCE_PROTOCOL = "web_auto"
AUTO_MODE_SMART = "smart"
AUTO_MODE_CUSTOM = "custom"
AUTO_DISPATCH_PRIORITY = "priority"
AUTO_DISPATCH_BALANCED = "balanced"
WEB_SOURCE_PRIORITY = ("web-deepseek", "web-qwen", "web-doubao", "web-kimi", "web-perplexity", "web-wenxin", "web-longcat", "web-yuanbao")
SUPPORTED_WEB_PROTOCOLS = {"deepseek_web", "qwen_web", "doubao_web", "kimi_web", "perplexity_web", "wenxin_web", "longcat_web"}
AUTO_ELIGIBLE_HEALTH = {"healthy", "unchecked"}
DEFAULT_WEB_CONCURRENCY = 3
MAX_WEB_CONCURRENCY = 32
WEB_HEALTH_COOLDOWN_SECONDS = 300


def source_concurrency(source: dict[str, Any]) -> int:
    raw = (source.get("config") or {}).get("max_concurrency", DEFAULT_WEB_CONCURRENCY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_WEB_CONCURRENCY
    return max(1, min(MAX_WEB_CONCURRENCY, value))


def batch_health_candidates(
    sources: list[dict[str, Any]],
    *,
    force: bool,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    current = now or datetime.now(UTC)
    candidates: list[dict[str, Any]] = []
    skipped = 0
    for source in sources:
        if source.get("kind") != "web" or source.get("id") == AUTO_SOURCE_ID:
            continue
        if not source.get("has_credential") or (not force and not source.get("enabled")):
            skipped += 1
            continue
        if not force and source.get("last_checked_at"):
            try:
                checked_at = datetime.fromisoformat(source["last_checked_at"])
                if checked_at.tzinfo is None:
                    checked_at = checked_at.replace(tzinfo=UTC)
                if (current - checked_at).total_seconds() < WEB_HEALTH_COOLDOWN_SECONDS:
                    skipped += 1
                    continue
            except (TypeError, ValueError):
                pass
        candidates.append(source)
    return candidates, skipped


@dataclass
class SourceLease:
    scheduler: "SourceScheduler"
    source_id: str
    released: bool = False

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        await self.scheduler.release(self.source_id)


class SourceScheduler:
    """Per-source concurrency limits. There is deliberately no global limit."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active: dict[str, int] = {}
        self._waiting: dict[str, int] = {}
        self._auto_waiting = 0
        self._balanced_cursor = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": dict(self._active),
            "waiting": dict(self._waiting),
            "auto_waiting": self._auto_waiting,
        }

    async def acquire(self, source_id: str, limit: int) -> SourceLease:
        async with self._condition:
            self._waiting[source_id] = self._waiting.get(source_id, 0) + 1
            try:
                while self._active.get(source_id, 0) >= limit:
                    await self._condition.wait()
                self._active[source_id] = self._active.get(source_id, 0) + 1
                return SourceLease(self, source_id)
            finally:
                remaining = self._waiting.get(source_id, 1) - 1
                if remaining > 0:
                    self._waiting[source_id] = remaining
                else:
                    self._waiting.pop(source_id, None)

    async def acquire_first(self, candidates: list[tuple[str, int]]) -> SourceLease:
        if not candidates:
            raise ProviderError("WebAuto 当前没有可用的 Web 来源", "no_available_source", 503)
        async with self._condition:
            self._auto_waiting += 1
            try:
                while True:
                    for source_id, limit in candidates:
                        if self._active.get(source_id, 0) < limit:
                            self._active[source_id] = self._active.get(source_id, 0) + 1
                            return SourceLease(self, source_id)
                    await self._condition.wait()
            finally:
                self._auto_waiting -= 1

    async def acquire_balanced(self, candidates: list[tuple[str, int]]) -> SourceLease:
        if not candidates:
            raise ProviderError("WebAuto 当前没有可用的 Web 来源", "no_available_source", 503)
        async with self._condition:
            self._auto_waiting += 1
            try:
                while True:
                    start = self._balanced_cursor % len(candidates)
                    for offset in range(len(candidates)):
                        index = (start + offset) % len(candidates)
                        source_id, limit = candidates[index]
                        if self._active.get(source_id, 0) < limit:
                            self._active[source_id] = self._active.get(source_id, 0) + 1
                            self._balanced_cursor = (index + 1) % len(candidates)
                            return SourceLease(self, source_id)
                    await self._condition.wait()
            finally:
                self._auto_waiting -= 1

    async def release(self, source_id: str) -> None:
        async with self._condition:
            remaining = self._active.get(source_id, 0) - 1
            if remaining > 0:
                self._active[source_id] = remaining
            else:
                self._active.pop(source_id, None)
            self._condition.notify_all()


@dataclass
class RouteContext:
    requested_source: dict[str, Any]
    actual_source: dict[str, Any] | None = None
    queue_ms: int = 0
    fallback_errors: list[str] = field(default_factory=list)
    lease: SourceLease | None = field(default=None, repr=False)

    @property
    def source_label(self) -> str:
        if self.requested_source.get("protocol") != AUTO_SOURCE_PROTOCOL:
            return (self.actual_source or self.requested_source).get("name", "-")
        if self.actual_source:
            return f"WebAuto → {self.actual_source['name']}"
        return "WebAuto"

    @property
    def note(self) -> str | None:
        parts: list[str] = []
        if self.queue_ms >= 100:
            parts.append(f"排队 {self.queue_ms} ms")
        if self.fallback_errors:
            parts.append("；".join(self.fallback_errors))
        return " · ".join(parts) or None


StreamFactory = Callable[[dict[str, Any], CanonicalRequest], AsyncIterator[CanonicalEvent]]


class WebRouter:
    def __init__(
        self,
        storage: Storage,
        scheduler: SourceScheduler,
        stream_factory: StreamFactory = stream_source,
        keepalive_seconds: float = 12.0,
    ) -> None:
        self.storage = storage
        self.scheduler = scheduler
        self.stream_factory = stream_factory
        self.keepalive_seconds = keepalive_seconds

    def _record_source_failure(self, source: dict[str, Any], exc: Exception) -> None:
        status = exc.status if isinstance(exc, ProviderError) else "upstream_error"
        message = str(exc).strip() or type(exc).__name__
        self.storage.update_health(source["id"], status, message)

    async def _prepare_source(self, source: dict[str, Any]) -> None:
        if self.stream_factory is stream_source and await refresh_kimi_credential(source):
            self.storage.update_source_credential(source["id"], source["credential"])

    @staticmethod
    def _routing_policy(sources: dict[str, dict[str, Any]]) -> tuple[str, list[str], str]:
        config = (sources.get(AUTO_SOURCE_ID) or {}).get("config") or {}
        mode = AUTO_MODE_CUSTOM if config.get("routing_mode") == AUTO_MODE_CUSTOM else AUTO_MODE_SMART
        dispatch = AUTO_DISPATCH_BALANCED if config.get("dispatch_mode") == AUTO_DISPATCH_BALANCED else AUTO_DISPATCH_PRIORITY
        source_ids = config.get("source_ids") if isinstance(config.get("source_ids"), list) else []
        selected = list(dict.fromkeys(
            source_id for source_id in source_ids
            if isinstance(source_id, str) and source_id != AUTO_SOURCE_ID
        ))
        return mode, selected, dispatch

    def routing_settings(self) -> tuple[str, str]:
        sources = {item["id"]: item for item in self.storage.list_sources()}
        mode, _, dispatch = self._routing_policy(sources)
        return mode, dispatch

    def configured_candidates(self, *, require_healthy: bool = True) -> list[dict[str, Any]]:
        sources = {item["id"]: item for item in self.storage.list_sources(include_secret=True)}
        mode, selected_ids, _ = self._routing_policy(sources)
        result: list[dict[str, Any]] = []
        source_ids = selected_ids if mode == AUTO_MODE_CUSTOM else WEB_SOURCE_PRIORITY
        for source_id in source_ids:
            source = sources.get(source_id)
            if not source or source.get("kind") != "web" or source_id == AUTO_SOURCE_ID:
                continue
            if mode == AUTO_MODE_SMART:
                if not source.get("enabled") or source.get("protocol") not in SUPPORTED_WEB_PROTOCOLS:
                    continue
                if not source.get("credential"):
                    continue
                if require_healthy and source.get("health_status") not in AUTO_ELIGIBLE_HEALTH:
                    continue
                model = next((item for item in source.get("models") or [] if item.get("enabled")), None)
            else:
                model = next(iter(source.get("models") or []), None)
            if not model:
                continue
            source["route_model"] = model
            result.append(source)
        return result

    def runtime_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshot = self.scheduler.snapshot()
        active = snapshot["active"]
        waiting = snapshot["waiting"]
        public_by_id = {source["id"]: source for source in sources}
        mode, selected_ids, dispatch = self._routing_policy(public_by_id)
        candidates: list[dict[str, Any]] = []
        source_ids = selected_ids if mode == AUTO_MODE_CUSTOM else WEB_SOURCE_PRIORITY
        for source_id in source_ids:
            source = public_by_id.get(source_id)
            if not source or source.get("kind") != "web" or source_id == AUTO_SOURCE_ID:
                continue
            if mode == AUTO_MODE_SMART:
                if not source.get("enabled") or not source.get("has_credential"):
                    continue
                if source.get("protocol") not in SUPPORTED_WEB_PROTOCOLS or source.get("health_status") not in AUTO_ELIGIBLE_HEALTH:
                    continue
            candidates.append(source)

        for source in sources:
            if source.get("kind") != "web" or source.get("id") == AUTO_SOURCE_ID:
                continue
            source["runtime"] = {
                "active": active.get(source["id"], 0),
                "queued": waiting.get(source["id"], 0),
                "limit": source_concurrency(source),
            }

        auto = public_by_id.get(AUTO_SOURCE_ID)
        if auto:
            total_active = sum(active.get(source["id"], 0) for source in candidates)
            total_limit = sum(source_concurrency(source) for source in candidates)
            preferred = candidates[0] if candidates else None
            auto["runtime"] = {
                "active": total_active,
                "queued": snapshot["auto_waiting"],
                "limit": total_limit,
                "sources": len(candidates),
            }
            if preferred:
                dispatch_label = "均衡轮询" if dispatch == AUTO_DISPATCH_BALANCED else "优先来源"
                if mode == AUTO_MODE_CUSTOM:
                    auto["health_status"] = preferred.get("health_status") or "unchecked"
                    auto["health_message"] = f"自定义 · {dispatch_label} · {len(candidates)} 个来源（不按健康状态切换）"
                else:
                    auto["health_status"] = "healthy" if preferred.get("health_status") == "healthy" else "unchecked"
                    auto["health_message"] = f"智能 · {dispatch_label} · {len(candidates)} 个来源可参与路由"
                auto["last_checked_at"] = preferred.get("last_checked_at")
            else:
                auto["health_status"] = "unconfigured"
                auto["health_message"] = "自定义名单为空" if mode == AUTO_MODE_CUSTOM else "没有已启用、已配置且状态可用的 Web 来源"
                auto["last_checked_at"] = None
        return sources

    async def _queued_heartbeats(self, acquire_task: asyncio.Task[SourceLease], holder: dict[str, Any]) -> AsyncIterator[CanonicalEvent]:
        try:
            while not acquire_task.done():
                done, _ = await asyncio.wait({acquire_task}, timeout=self.keepalive_seconds)
                if not done:
                    yield CanonicalEvent("keepalive")
            holder["lease"] = await acquire_task
        except BaseException:
            if not acquire_task.done():
                acquire_task.cancel()
                await asyncio.gather(acquire_task, return_exceptions=True)
            elif not acquire_task.cancelled() and acquire_task.exception() is None:
                await acquire_task.result().release()
            raise

    async def _acquire_with_heartbeats(
        self,
        acquire: asyncio.Task[SourceLease],
        context: RouteContext,
    ) -> AsyncIterator[CanonicalEvent]:
        started = time.perf_counter()
        holder: dict[str, Any] = {}
        async for heartbeat in self._queued_heartbeats(acquire, holder):
            yield heartbeat
        context.queue_ms += int((time.perf_counter() - started) * 1000)
        context.lease = holder["lease"]

    async def stream(
        self,
        source: dict[str, Any],
        req: CanonicalRequest,
        context: RouteContext,
    ) -> AsyncIterator[CanonicalEvent]:
        if source.get("kind") != "web":
            context.actual_source = source
            async for event in self.stream_factory(source, req):
                yield event
            return

        if source.get("protocol") != AUTO_SOURCE_PROTOCOL:
            task = asyncio.create_task(self.scheduler.acquire(source["id"], source_concurrency(source)))
            async for heartbeat in self._acquire_with_heartbeats(task, context):
                yield heartbeat
            lease = context.lease
            if lease is None:
                raise ProviderError("来源并发调度失败", "scheduler_error", 500)
            context.actual_source = source
            try:
                await self._prepare_source(source)
                async for event in self.stream_factory(source, req):
                    yield event
            except Exception as exc:
                self._record_source_failure(source, exc)
                raise
            finally:
                await lease.release()
            return

        routing_mode, dispatch_mode = self.routing_settings()
        custom_mode = routing_mode == AUTO_MODE_CUSTOM
        excluded: set[str] = set()
        while True:
            candidates = [item for item in self.configured_candidates() if item["id"] not in excluded]
            if not candidates:
                detail = "；".join(context.fallback_errors)
                message = "WebAuto 当前没有可用的 Web 来源"
                if detail:
                    message += f"：{detail}"
                raise ProviderError(message, "no_available_source", 503)

            candidate_limits = [(item["id"], source_concurrency(item)) for item in candidates]
            acquire = self.scheduler.acquire_balanced(candidate_limits) if dispatch_mode == AUTO_DISPATCH_BALANCED else self.scheduler.acquire_first(candidate_limits)
            task = asyncio.create_task(acquire)
            async for heartbeat in self._acquire_with_heartbeats(task, context):
                yield heartbeat
            lease = context.lease
            if lease is None:
                raise ProviderError("WebAuto 并发调度失败", "scheduler_error", 500)
            candidate = next(item for item in candidates if item["id"] == lease.source_id)
            context.actual_source = candidate
            candidate_req = replace(req, upstream_model=candidate["route_model"]["upstream_name"])
            stream_task: asyncio.Task[list[CanonicalEvent]] | None = None
            buffered: list[CanonicalEvent] = []
            try:
                await self._prepare_source(candidate)
                async def collect_candidate() -> list[CanonicalEvent]:
                    return [
                        event
                        async for event in self.stream_factory(candidate, candidate_req)
                        if event.type != "keepalive"
                    ]

                stream_task = asyncio.create_task(collect_candidate())
                while not stream_task.done():
                    done, _ = await asyncio.wait({stream_task}, timeout=self.keepalive_seconds)
                    if not done:
                        yield CanonicalEvent("keepalive")
                buffered = await stream_task
            except Exception as exc:
                self._record_source_failure(candidate, exc)
                if custom_mode:
                    raise
                excluded.add(candidate["id"])
                context.fallback_errors.append(f"{candidate['name']}：{str(exc)[:120]}")
            finally:
                if stream_task is not None and not stream_task.done():
                    stream_task.cancel()
                    await asyncio.gather(stream_task, return_exceptions=True)
                await lease.release()
            if buffered:
                for event in buffered:
                    yield event
                return
            if stream_task is not None and stream_task.done() and stream_task.exception() is None:
                return
