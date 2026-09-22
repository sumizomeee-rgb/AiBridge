from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.protocols import CanonicalRequest
from aibridge.providers import _stream_qwen


class QwenStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_adds_required_version_and_fresh_request_ids(self) -> None:
        source = {
            "base_url": "https://chat.qwen.ai",
            "credential": {
                "request_url": "https://chat.qwen.ai/api/v2/chat/completions?chat_id=old",
                "headers": {"X-Request-Id": "stale", "content-type": "application/json"},
                "cookie": "session=test",
            },
        }
        request = CanonicalRequest(
            model="qwen-web",
            upstream_model="qwen3.7-plus",
            messages=[{"role": "user", "content": "只回复 OK"}],
        )
        responses = [
            (200, json.dumps({"data": {"id": "new-chat"}})),
            (200, 'data: {"choices":[{"delta":{"content":"OK","phase":"answer"}}]}\n\n'),
        ]
        mocked_post = AsyncMock(side_effect=responses)

        with patch("aibridge.providers._curl_post", mocked_post):
            events = [event async for event in _stream_qwen(source, request)]

        self.assertEqual("".join(event.text for event in events), "OK")
        first_headers = mocked_post.await_args_list[0].args[1]
        second_headers = mocked_post.await_args_list[1].args[1]
        self.assertEqual(first_headers["Version"], "0.2.91")
        self.assertEqual(second_headers["Version"], "0.2.91")
        self.assertNotEqual(first_headers["X-Request-Id"], "stale")
        self.assertNotEqual(first_headers["X-Request-Id"], second_headers["X-Request-Id"])


if __name__ == "__main__":
    unittest.main()
