from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.protocols import CanonicalEvent, CanonicalRequest, collect_events
from aibridge.providers import ProviderError
from aibridge.routing import RouteContext, SourceScheduler, WebRouter, batch_health_candidates
from aibridge.storage import Storage


def web_source(source_id: str, name: str, protocol: str, model: str) -> dict:
    return {
        "id": source_id,
        "name": name,
        "kind": "web",
        "protocol": protocol,
        "enabled": True,
        "health_status": "healthy",
        "has_credential": True,
        "credential": {"headers": {"authorization": "test"}},
        "config": {"max_concurrency": 3},
        "models": [{"public_name": model, "upstream_name": "default", "enabled": True}],
    }


class FakeStorage:
    def __init__(self) -> None:
        self.sources = [
            {
                "id": "web-auto", "name": "WebAuto", "kind": "web", "protocol": "web_auto",
                "enabled": True, "health_status": "unchecked", "has_credential": False,
                "credential": {}, "config": {"routing_mode": "smart", "dispatch_mode": "priority", "source_ids": []},
                "models": [{"public_name": "web-auto", "upstream_name": "auto", "enabled": True}],
            },
            web_source("web-deepseek", "DeepSeek Web", "deepseek_web", "deepseek-web"),
            web_source("web-qwen", "千问 Web", "qwen_web", "qwen-web"),
            web_source("web-doubao", "豆包 Web", "doubao_web", "doubao-web"),
        ]
        self.health_updates: list[tuple[str, str, str]] = []

    def set_custom(self, source_ids: list[str]) -> None:
        self.sources[0]["config"] = {"routing_mode": "custom", "dispatch_mode": "priority", "source_ids": source_ids}

    def list_sources(self, include_secret: bool = False) -> list[dict]:
        sources = copy.deepcopy(self.sources)
        if not include_secret:
            for source in sources:
                source.pop("credential", None)
        return sources

    def update_health(self, source_id: str, status: str, message: str) -> None:
        self.health_updates.append((source_id, status, message))


class SourceSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_limits_each_source_without_global_cap(self) -> None:
        scheduler = SourceScheduler()
        deepseek = [await scheduler.acquire("web-deepseek", 3) for _ in range(3)]
        fourth = asyncio.create_task(scheduler.acquire("web-deepseek", 3))
        await asyncio.sleep(0)
        self.assertFalse(fourth.done())
        self.assertEqual(scheduler.snapshot()["waiting"]["web-deepseek"], 1)

        qwen = await asyncio.wait_for(scheduler.acquire("web-qwen", 3), timeout=0.1)
        self.assertEqual(scheduler.snapshot()["active"], {"web-deepseek": 3, "web-qwen": 1})

        await deepseek.pop().release()
        acquired = await asyncio.wait_for(fourth, timeout=0.1)
        await acquired.release()
        await qwen.release()
        for lease in deepseek:
            await lease.release()

    async def test_auto_uses_next_source_when_priority_source_is_full(self) -> None:
        scheduler = SourceScheduler()
        deepseek = [await scheduler.acquire("web-deepseek", 3) for _ in range(3)]
        lease = await asyncio.wait_for(
            scheduler.acquire_first([("web-deepseek", 3), ("web-qwen", 3)]),
            timeout=0.1,
        )
        self.assertEqual(lease.source_id, "web-qwen")
        await lease.release()
        for item in deepseek:
            await item.release()

    async def test_balanced_dispatch_rotates_even_after_each_request_finishes(self) -> None:
        scheduler = SourceScheduler()
        candidates = [("web-deepseek", 3), ("web-qwen", 3), ("web-doubao", 3)]
        selected: list[str] = []
        for _ in range(6):
            lease = await scheduler.acquire_balanced(candidates)
            selected.append(lease.source_id)
            await lease.release()

        self.assertEqual(selected, ["web-deepseek", "web-qwen", "web-doubao", "web-deepseek", "web-qwen", "web-doubao"])


class WebRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_falls_back_before_first_content(self) -> None:
        storage = FakeStorage()

        async def fake_stream(source: dict, request: CanonicalRequest):
            if source["id"] == "web-deepseek":
                raise ProviderError("临时协议异常", "protocol_mismatch")
            yield CanonicalEvent("text", text=f"from:{source['id']}:{request.upstream_model}")

        router = WebRouter(storage, SourceScheduler(), fake_stream, keepalive_seconds=0.01)
        auto = {"id": "web-auto", "name": "WebAuto", "kind": "web", "protocol": "web_auto"}
        context = RouteContext(auto)
        request = CanonicalRequest(model="web-auto", upstream_model="auto", messages=[])

        text, _, _ = await collect_events(router.stream(auto, request, context))
        self.assertEqual(text, "from:web-qwen:default")
        self.assertEqual(context.actual_source["id"], "web-qwen")
        self.assertIn("DeepSeek Web：临时协议异常", context.note)
        self.assertEqual(storage.health_updates, [("web-deepseek", "protocol_mismatch", "临时协议异常")])
        self.assertEqual(router.scheduler.snapshot()["active"], {})

    async def test_auto_discards_partial_content_and_falls_back(self) -> None:
        storage = FakeStorage()
        calls: list[str] = []

        async def fake_stream(source: dict, request: CanonicalRequest):
            calls.append(source["id"])
            if source["id"] == "web-deepseek":
                yield CanonicalEvent("text", text="不应泄漏的残稿")
                raise ProviderError("流式连接中断", "upstream_error")
            yield CanonicalEvent("text", text="完整回答")

        router = WebRouter(storage, SourceScheduler(), fake_stream, keepalive_seconds=0.01)
        auto = storage.sources[0]
        request = CanonicalRequest(model="web-auto", upstream_model="auto", messages=[])

        text, _, _ = await collect_events(router.stream(auto, request, RouteContext(auto)))

        self.assertEqual("完整回答", text)
        self.assertEqual(["web-deepseek", "web-qwen"], calls)
        self.assertNotIn("不应泄漏的残稿", text)
        self.assertEqual(storage.health_updates, [("web-deepseek", "upstream_error", "流式连接中断")])
        self.assertEqual(router.scheduler.snapshot()["active"], {})

    async def test_auto_sends_keepalive_while_buffering_complete_answer(self) -> None:
        storage = FakeStorage()

        async def fake_stream(source: dict, request: CanonicalRequest):
            yield CanonicalEvent("text", text="前半段")
            await asyncio.sleep(0.03)
            yield CanonicalEvent("text", text="后半段")

        router = WebRouter(storage, SourceScheduler(), fake_stream, keepalive_seconds=0.005)
        auto = storage.sources[0]
        request = CanonicalRequest(model="web-auto", upstream_model="auto", messages=[])

        events = [event async for event in router.stream(auto, request, RouteContext(auto))]

        self.assertEqual("keepalive", events[0].type)
        self.assertEqual("前半段后半段", "".join(event.text for event in events if event.type == "text"))
        self.assertEqual(router.scheduler.snapshot()["active"], {})

    async def test_auto_balanced_dispatch_rotates_real_routes(self) -> None:
        storage = FakeStorage()
        storage.sources[0]["config"]["dispatch_mode"] = "balanced"
        selected: list[str] = []

        async def fake_stream(source: dict, request: CanonicalRequest):
            selected.append(source["id"])
            yield CanonicalEvent("text", text=source["id"])

        router = WebRouter(storage, SourceScheduler(), fake_stream, keepalive_seconds=0.01)
        auto = storage.sources[0]
        request = CanonicalRequest(model="web-auto", upstream_model="auto", messages=[])
        for _ in range(4):
            await collect_events(router.stream(auto, request, RouteContext(auto)))

        self.assertEqual(selected, ["web-deepseek", "web-qwen", "web-doubao", "web-deepseek"])

    async def test_direct_web_failure_updates_source_health_immediately(self) -> None:
        storage = FakeStorage()

        async def fake_stream(source: dict, request: CanonicalRequest):
            raise ProviderError("Authorization Failed (invalid token)", "auth_expired")
            yield

        router = WebRouter(storage, SourceScheduler(), fake_stream, keepalive_seconds=0.01)
        deepseek = storage.sources[1]
        context = RouteContext(deepseek)
        request = CanonicalRequest(model="deepseek-web", upstream_model="default", messages=[])

        with self.assertRaisesRegex(ProviderError, "invalid token"):
            await collect_events(router.stream(deepseek, request, context))
        self.assertEqual(storage.health_updates, [("web-deepseek", "auth_expired", "Authorization Failed (invalid token)")])
        self.assertEqual(router.scheduler.snapshot()["active"], {})

    async def test_custom_route_keeps_disabled_unhealthy_source_and_exact_order(self) -> None:
        storage = FakeStorage()
        qwen = next(source for source in storage.sources if source["id"] == "web-qwen")
        qwen["enabled"] = False
        qwen["health_status"] = "auth_expired"
        qwen["credential"] = {}
        storage.set_custom(["web-qwen", "web-deepseek"])

        router = WebRouter(storage, SourceScheduler())
        candidates = router.configured_candidates()

        self.assertEqual([source["id"] for source in candidates], ["web-qwen", "web-deepseek"])

    async def test_smart_route_skips_unhealthy_source(self) -> None:
        storage = FakeStorage()
        qwen = next(source for source in storage.sources if source["id"] == "web-qwen")
        qwen["health_status"] = "auth_expired"

        router = WebRouter(storage, SourceScheduler())
        candidates = router.configured_candidates()

        self.assertNotIn("web-qwen", [source["id"] for source in candidates])

    async def test_custom_route_never_falls_back_after_selected_source_fails(self) -> None:
        storage = FakeStorage()
        storage.set_custom(["web-deepseek", "web-qwen"])
        calls: list[str] = []

        async def fake_stream(source: dict, request: CanonicalRequest):
            calls.append(source["id"])
            if source["id"] == "web-deepseek":
                raise ProviderError("固定来源失败", "protocol_mismatch")
            yield CanonicalEvent("text", text="unexpected fallback")

        router = WebRouter(storage, SourceScheduler(), fake_stream, keepalive_seconds=0.01)
        auto = storage.sources[0]
        context = RouteContext(auto)
        request = CanonicalRequest(model="web-auto", upstream_model="auto", messages=[])

        with self.assertRaisesRegex(ProviderError, "固定来源失败"):
            await collect_events(router.stream(auto, request, context))
        self.assertEqual(calls, ["web-deepseek"])
        self.assertEqual(storage.health_updates, [("web-deepseek", "protocol_mismatch", "固定来源失败")])
        self.assertEqual(router.scheduler.snapshot()["active"], {})


class StorageSeedTests(unittest.TestCase):
    def test_web_auto_order_and_default_source_concurrency(self) -> None:
        with TemporaryDirectory(prefix="aibridge-routing-") as temp:
            root = Path(temp)
            storage = Storage(root / "aibridge.db", root / "secret.key")
            sources = storage.list_sources()
            self.assertEqual(
                [source["id"] for source in sources[:9]],
                ["web-auto", "web-deepseek", "web-qwen", "web-doubao", "web-kimi", "web-perplexity", "web-wenxin", "web-longcat", "web-yuanbao"],
            )
            auto = sources[0]
            self.assertEqual(auto["models"][0]["public_name"], "web-auto")
            self.assertEqual(auto["config"]["routing_mode"], "smart")
            self.assertEqual(auto["config"]["dispatch_mode"], "priority")
            self.assertEqual(auto["config"]["source_ids"], [])
            kimi = next(source for source in sources if source["id"] == "web-kimi")
            self.assertEqual(kimi["protocol"], "kimi_web")
            self.assertEqual(kimi["models"][0]["upstream_name"], "k2d6-chat")
            perplexity = next(source for source in sources if source["id"] == "web-perplexity")
            self.assertEqual(perplexity["protocol"], "perplexity_web")
            self.assertEqual(perplexity["models"][0]["upstream_name"], "turbo")
            wenxin = next(source for source in sources if source["id"] == "web-wenxin")
            self.assertEqual(wenxin["protocol"], "wenxin_web")
            self.assertEqual(wenxin["models"][0]["upstream_name"], "smartMode")
            longcat = next(source for source in sources if source["id"] == "web-longcat")
            self.assertEqual(longcat["protocol"], "longcat_web")
            self.assertEqual(longcat["models"][0]["upstream_name"], "LongCat-2.0-Preview-LongCatAI")
            self.assertEqual(longcat["config"]["max_concurrency"], 1)
            for source in sources[1:7]:
                self.assertEqual(source["config"]["max_concurrency"], 3)


class BatchHealthCandidatesTests(unittest.TestCase):
    def test_auto_check_uses_enabled_stale_sources_only(self) -> None:
        now = datetime(2026, 9, 22, 4, 0, tzinfo=UTC)
        sources = [
            {"id": "web-auto", "kind": "web", "enabled": True, "has_credential": False},
            {"id": "web-deepseek", "kind": "web", "enabled": True, "has_credential": True, "last_checked_at": (now - timedelta(minutes=1)).isoformat()},
            {"id": "web-qwen", "kind": "web", "enabled": False, "has_credential": True, "last_checked_at": (now - timedelta(minutes=10)).isoformat()},
            {"id": "web-kimi", "kind": "web", "enabled": True, "has_credential": True, "last_checked_at": (now - timedelta(minutes=10)).isoformat()},
            {"id": "web-doubao", "kind": "web", "enabled": True, "has_credential": False},
        ]

        candidates, skipped = batch_health_candidates(sources, force=False, now=now)

        self.assertEqual([source["id"] for source in candidates], ["web-kimi"])
        self.assertEqual(skipped, 3)

    def test_manual_check_includes_disabled_configured_sources(self) -> None:
        sources = [
            {"id": "web-qwen", "kind": "web", "enabled": False, "has_credential": True},
            {"id": "web-doubao", "kind": "web", "enabled": False, "has_credential": False},
        ]

        candidates, skipped = batch_health_candidates(sources, force=True)

        self.assertEqual([source["id"] for source in candidates], ["web-qwen"])
        self.assertEqual(skipped, 1)


if __name__ == "__main__":
    unittest.main()
