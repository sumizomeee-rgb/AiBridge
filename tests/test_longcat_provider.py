from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.protocols import CanonicalRequest
from aibridge.providers import ProviderError, _longcat_stream_diagnostic, _parse_longcat_stream, _stream_longcat
from aibridge.relay import RelayError


LONGCAT_STREAM = """data: {"event":{"type":"create"},"messageId":2,"lastOne":false}

data: {"event":{"type":"content","content":"你","status":"PROCESSING"},"messageId":2,"lastOne":false}

data: {"event":{"type":"content","content":"好","status":"PROCESSING"},"messageId":2,"lastOne":false}

data: {"event":{"type":"finish","finalContentX":"你好","usage":{"inputTokens":12,"outputTokens":2,"totalTokens":14}},"messageId":2,"lastOne":true}

"""


class LongCatProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_text_and_usage(self) -> None:
        events = _parse_longcat_stream(LONGCAT_STREAM)
        self.assertEqual("".join(event.text for event in events if event.type == "text"), "你好")
        usage = next(event.usage for event in events if event.type == "usage")
        self.assertEqual(usage, {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14})

    def test_diagnostic_reports_protocol_shape_without_response_text(self) -> None:
        diagnostic = _longcat_stream_diagnostic('data: {"event":{"type":"create"}}\n\ndata: invalid\n')
        self.assertIn("SSE 数据 2 条", diagnostic)
        self.assertIn("事件类型 create", diagnostic)
        self.assertIn("无法解析 1 条", diagnostic)

        json_diagnostic = _longcat_stream_diagnostic('{"code":429,"message":"too many requests"}')
        self.assertIn("code=429", json_diagnostic)
        self.assertIn("message=too many requests", json_diagnostic)

    async def test_stream_sends_flattened_prompt_through_browser_relay(self) -> None:
        source = {
            "credential": {
                "browser_relay": True,
                "headers": {"m-appkey": "app", "mtgsig": "must-not-forward"},
            }
        }
        request = CanonicalRequest(
            model="longcat-web",
            upstream_model="LongCat-2.0-Preview-LongCatAI",
            system="遵守要求",
            messages=[{"role": "user", "content": "你好"}],
        )
        relay = AsyncMock(return_value={"status": 200, "body": LONGCAT_STREAM})

        with patch("aibridge.providers.relay_broker.request", relay):
            events = [event async for event in _stream_longcat(source, request)]

        self.assertEqual("".join(event.text for event in events if event.type == "text"), "你好")
        source_id, action, payload = relay.await_args.args
        self.assertEqual((source_id, action), ("web-longcat", "longcat_chat"))
        self.assertIn("遵守要求", payload["prompt"])
        self.assertIn("你好", payload["prompt"])
        self.assertEqual(payload["headers"], {"m-appkey": "app"})

    async def test_stream_reports_offline_relay(self) -> None:
        source = {"credential": {"browser_relay": True}}
        request = CanonicalRequest(model="longcat-web", upstream_model="LongCat", messages=[])
        with patch("aibridge.providers.relay_broker.request", AsyncMock(side_effect=RelayError("中继离线"))):
            with self.assertRaisesRegex(ProviderError, "中继离线") as caught:
                [event async for event in _stream_longcat(source, request)]
        self.assertEqual(caught.exception.status, "browser_relay_unavailable")

    async def test_stream_classifies_meituan_verification_as_challenge(self) -> None:
        source = {"credential": {"browser_relay": True}}
        request = CanonicalRequest(model="longcat-web", upstream_model="LongCat", messages=[])
        response = {"status": 418, "stage": "chat-completion", "body": '{"customData":{"riskLevel":"199"}}'}
        with patch("aibridge.providers.relay_broker.request", AsyncMock(return_value=response)):
            with self.assertRaisesRegex(ProviderError, "安全验证") as caught:
                [event async for event in _stream_longcat(source, request)]
        self.assertEqual(caught.exception.status, "challenge")


if __name__ == "__main__":
    unittest.main()
