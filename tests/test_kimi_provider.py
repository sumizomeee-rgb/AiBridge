from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.protocols import CanonicalRequest
from aibridge.providers import ProviderError, _connect_message, _kimi_delta, _pop_connect_frame, _stream_kimi


class KimiConnectTests(unittest.TestCase):
    def test_connect_frame_can_arrive_in_parts(self) -> None:
        framed = _connect_message({"mask": "block.text", "op": "set", "block": {"text": {"content": "你好"}}})
        buffer = bytearray(framed[:7])
        self.assertIsNone(_pop_connect_frame(buffer))
        buffer.extend(framed[7:])

        flags, message = _pop_connect_frame(buffer) or (-1, None)
        self.assertEqual(flags, 0)
        self.assertEqual(message["block"]["text"]["content"], "你好")
        self.assertEqual(buffer, bytearray())

    def test_kimi_text_and_reasoning_deltas(self) -> None:
        text = _kimi_delta({"op": "append", "mask": "block.text.content", "block": {"text": {"content": "答案"}}})
        reasoning = _kimi_delta({"op": "set", "mask": "block.think", "block": {"think": {"content": "思考"}}})

        self.assertEqual((text.type, text.text), ("text", "答案"))
        self.assertEqual((reasoning.type, reasoning.reasoning), ("reasoning", "思考"))

    def test_rejects_oversized_connect_frame(self) -> None:
        buffer = bytearray(b"\x00" + (8 * 1024 * 1024 + 1).to_bytes(4, "big"))
        with self.assertRaisesRegex(ProviderError, "超过 8 MiB"):
            _pop_connect_frame(buffer)


class KimiStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_uses_bearer_and_discards_dynamic_browser_headers(self) -> None:
        end_frame = bytearray(_connect_message({}))
        end_frame[0] = 0x02
        response_bytes = b"".join([
            _connect_message({"op": "set", "mask": "block.think", "block": {"think": {"content": "思"}}}),
            _connect_message({"op": "set", "mask": "block.text", "block": {"text": {"content": "答"}}}),
            bytes(end_frame),
        ])

        class FakeResponse:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def aiter_bytes(self):
                yield response_bytes[:9]
                yield response_bytes[9:]

        class FakeClient:
            request: dict = {}

            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            def stream(self, method, url, **kwargs):
                self.__class__.request = {"method": method, "url": url, **kwargs}
                return FakeResponse()

        source = {
            "base_url": "https://www.kimi.com",
            "credential": {
                "request_url": "https://www.kimi.com/apiv2/kimi.gateway.chat.v1.ChatService/Chat",
                "headers": {
                    "authorization": "Bearer test-token",
                    "x-msh-shield-data": "stale",
                    "x-traffic-id": "stale",
                },
                "cookie": "unused=value",
            },
        }
        request = CanonicalRequest(model="kimi-web", upstream_model="k2d6-chat", messages=[{"role": "user", "content": "你好"}])

        with patch("aibridge.providers.httpx.AsyncClient", FakeClient):
            events = [event async for event in _stream_kimi(source, request)]

        self.assertEqual([(event.type, event.text or event.reasoning) for event in events], [("reasoning", "思"), ("text", "答")])
        headers = {key.lower(): value for key, value in FakeClient.request["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer test-token")
        self.assertNotIn("cookie", headers)
        self.assertNotIn("x-msh-shield-data", headers)
        self.assertNotIn("x-traffic-id", headers)
        body_buffer = bytearray(FakeClient.request["content"])
        _, body = _pop_connect_frame(body_buffer) or (-1, None)
        self.assertEqual(body["options"]["model"], "k2d6-chat")
        self.assertIn("你好", body["message"]["blocks"][0]["text"]["content"])


if __name__ == "__main__":
    unittest.main()
