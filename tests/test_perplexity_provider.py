from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.protocols import CanonicalRequest
from aibridge.providers import ProviderError, _parse_perplexity_stream, _stream_perplexity


class PerplexityStreamTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def fixture() -> str:
        events = [
            {
                "blocks": [{
                    "diff_block": {
                        "field": "workflow_block",
                        "patches": [{
                            "op": "add",
                            "path": "/steps/1",
                            "value": {"items": [{"payload": {"text_payload": {"text": "你好", "chunks": ["你"]}}}]},
                        }],
                    }
                }]
            },
            {
                "blocks": [{
                    "diff_block": {
                        "field": "workflow_block",
                        "patches": [{"op": "add", "path": "/steps/1/items/0/payload/text_payload/chunks/1", "value": "好"}],
                    }
                }],
                "final": True,
            },
        ]
        return "\n\n".join(f"event: message\ndata: {json.dumps(event, ensure_ascii=False)}" for event in events)

    def test_parses_workflow_text_chunks(self) -> None:
        self.assertEqual(_parse_perplexity_stream(self.fixture()), ["你", "好"])

    def test_parses_markdown_chunks_without_repeating_final_snapshot(self) -> None:
        events = [
            {"blocks": [{"markdown_block": {"progress": "IN_PROGRESS", "chunks": ["你"], "chunk_starting_offset": 0}}]},
            {"blocks": [{"markdown_block": {"progress": "IN_PROGRESS", "chunks": ["好"], "chunk_starting_offset": 1}}]},
            {"blocks": [{"markdown_block": {"progress": "DONE", "chunks": ["你", "好"], "chunk_starting_offset": 0, "answer": "你好"}}]},
        ]
        raw = "\n\n".join(f"data: {json.dumps(event, ensure_ascii=False)}" for event in events)
        self.assertEqual(_parse_perplexity_stream(raw), ["你", "好"])

    async def test_stream_builds_fresh_request_and_returns_text(self) -> None:
        source = {
            "base_url": "https://www.perplexity.ai",
            "credential": {
                "request_url": "https://www.perplexity.ai/rest/sse/perplexity_ask",
                "headers": {"x-pplx-account": "account-id", "x-request-id": "stale"},
                "cookie": "session=test",
            },
        }
        request = CanonicalRequest(model="perplexity-web", upstream_model="turbo", messages=[{"role": "user", "content": "你好"}])
        mocked_post = AsyncMock(return_value=(200, self.fixture()))

        with patch("aibridge.providers._curl_post", mocked_post):
            events = [event async for event in _stream_perplexity(source, request)]

        self.assertEqual("".join(event.text for event in events), "你好")
        url, headers, body = mocked_post.await_args.args
        self.assertEqual(url, "https://www.perplexity.ai/rest/sse/perplexity_ask")
        self.assertEqual(headers["x-pplx-account"], "account-id")
        self.assertEqual(headers["cookie"], "session=test")
        self.assertNotEqual(headers["x-request-id"], "stale")
        self.assertEqual(body["params"]["model_preference"], "turbo")
        self.assertIn("你好", body["query_str"])

    async def test_sign_up_response_is_not_reported_as_healthy_text(self) -> None:
        raw = 'data: {"blocks":[{"markdown_block":{"progress":"DONE","chunks":["Sign up and repeat your request."],"chunk_starting_offset":0}}]}'
        source = {
            "base_url": "https://www.perplexity.ai",
            "credential": {
                "request_url": "https://www.perplexity.ai/rest/sse/perplexity_ask",
                "headers": {"x-pplx-account": "guest-id"},
            },
        }
        request = CanonicalRequest(model="perplexity-web", upstream_model="turbo", messages=[{"role": "user", "content": "你好"}])

        with patch("aibridge.providers._curl_post", AsyncMock(return_value=(200, raw))):
            with self.assertRaises(ProviderError) as caught:
                [event async for event in _stream_perplexity(source, request)]

        self.assertEqual(caught.exception.status, "auth_expired")


if __name__ == "__main__":
    unittest.main()
