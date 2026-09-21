from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.protocols import CanonicalEvent, CanonicalRequest, collect_events
from aibridge.providers import ProviderError
from aibridge.routing import RouteContext, SourceScheduler, WebRouter
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
            web_source("web-deepseek", "DeepSeek Web", "deepseek_web", "deepseek-web"),
            web_source("web-qwen", "千问 Web", "qwen_web", "qwen-web"),
            web_source("web-doubao", "豆包 Web", "doubao_web", "doubao-web"),
        ]
        self.health_updates: list[tuple[str, str, str]] = []

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
        self.assertEqual(router.scheduler.snapshot()["active"], {})


class StorageSeedTests(unittest.TestCase):
    def test_web_auto_order_and_default_source_concurrency(self) -> None:
        with TemporaryDirectory(prefix="aibridge-routing-") as temp:
            root = Path(temp)
            storage = Storage(root / "aibridge.db", root / "secret.key")
            sources = storage.list_sources()
            self.assertEqual(
                [source["id"] for source in sources[:6]],
                ["web-auto", "web-deepseek", "web-qwen", "web-doubao", "web-yuanbao", "web-kimi"],
            )
            auto = sources[0]
            self.assertEqual(auto["models"][0]["public_name"], "web-auto")
            for source in sources[1:6]:
                self.assertEqual(source["config"]["max_concurrency"], 3)


if __name__ == "__main__":
    unittest.main()
