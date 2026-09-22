from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.protocols import CanonicalRequest
from aibridge.providers import ProviderError, _mimo_stream_diagnostic, _parse_mimo_stream, _stream_mimo
from aibridge.runner import SensitiveQueryFilter


MIMO_STREAM = """id:test
event:dialogId
data:{"content":"30193626"}

event:message
data:{"type":"text","content":"<thi"}

event:message
data:{"type":"text","content":"nk>分析\\u0000过程"}

event:message
data:{"type":"text","content":"</think>\\u0000最终"}

event:message
data:{"type":"text","content":"答案"}

event:usage
data:{"promptTokens":12,"completionTokens":5,"totalTokens":17}

event:finish
data:{"content":"[DONE]"}

"""


class MiMoProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_log_filter_redacts_login_identifier(self) -> None:
        import logging

        record = logging.LogRecord(
            "httpx", logging.INFO, __file__, 1,
            'POST https://aistudio.xiaomimimo.com/open-apis/bot/chat?xiaomichatbot_ph=secret%2Bvalue%3D%3D "HTTP/2 200 OK"',
            (), None,
        )
        self.assertTrue(SensitiveQueryFilter().filter(record))
        self.assertNotIn("secret", record.getMessage())
        self.assertIn("xiaomichatbot_ph=[已脱敏]", record.getMessage())

    def test_parses_reasoning_text_and_usage_across_chunks(self) -> None:
        events = _parse_mimo_stream(MIMO_STREAM)
        self.assertEqual("".join(event.reasoning for event in events if event.type == "reasoning"), "分析过程")
        self.assertEqual("".join(event.text for event in events if event.type == "text"), "最终答案")
        usage = next(event.usage for event in events if event.type == "usage")
        self.assertEqual(usage, {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17})

    def test_diagnostic_reports_shape_without_response_text(self) -> None:
        diagnostic = _mimo_stream_diagnostic('event:message\ndata:{"type":"text","content":"秘密回答"}\n\nevent:usage\ndata:bad\n')
        self.assertIn("事件类型 message,usage", diagnostic)
        self.assertIn("无法解析 1 条", diagnostic)
        self.assertNotIn("秘密回答", diagnostic)

    async def test_stream_builds_fresh_stateless_request(self) -> None:
        source = {
            "credential": {
                "request_url": "https://aistudio.xiaomimimo.com/open-apis/bot/chat?xiaomichatbot_ph=test-ph",
                "headers": {"X-Timezone": "Asia/Shanghai", "Traceparent": "must-not-reuse"},
                "browser_relay": True,
            }
        }
        request = CanonicalRequest(
            model="mimo-web",
            upstream_model="mimo-v2.6-flash",
            system="遵守要求",
            messages=[{"role": "user", "content": "你好"}],
        )
        relay = AsyncMock(return_value={"status": 200, "body": MIMO_STREAM, "stage": "chat-completion"})

        with patch("aibridge.providers.relay_broker.request", relay):
            events = [event async for event in _stream_mimo(source, request)]

        self.assertEqual("".join(event.text for event in events if event.type == "text"), "最终答案")
        source_id, action, payload = relay.await_args.args
        self.assertEqual((source_id, action), ("web-mimo", "mimo_chat"))
        self.assertEqual(payload["model"], "mimo-v2.6-flash")
        self.assertEqual(payload["login_identifier"], "test-ph")
        self.assertEqual(payload["headers"], {"X-Timezone": "Asia/Shanghai"})
        self.assertIn("遵守要求", payload["prompt"])
        self.assertIn("你好", payload["prompt"])

    async def test_stream_rejects_missing_login_identifier(self) -> None:
        source = {"credential": {"request_url": "https://aistudio.xiaomimimo.com/open-apis/bot/chat", "browser_relay": True}}
        request = CanonicalRequest(model="mimo-web", upstream_model="mimo-v2.6-flash", messages=[])
        with self.assertRaisesRegex(ProviderError, "Catcher") as caught:
            [event async for event in _stream_mimo(source, request)]
        self.assertEqual(caught.exception.status, "invalid_config")

    async def test_stream_does_not_expose_login_redirect_on_auth_failure(self) -> None:
        source = {
            "credential": {
                "request_url": "https://aistudio.xiaomimimo.com/open-apis/bot/chat?xiaomichatbot_ph=test-ph",
                "browser_relay": True,
            }
        }
        request = CanonicalRequest(model="mimo-web", upstream_model="mimo-v2.6-flash", messages=[])
        response = '{"code":401,"loginUrl":"https://account.xiaomi.com/login?sign=secret"}'
        relay = AsyncMock(return_value={"status": 401, "body": response, "stage": "chat-completion"})
        with patch("aibridge.providers.relay_broker.request", relay):
            with self.assertRaises(ProviderError) as caught:
                [event async for event in _stream_mimo(source, request)]
        self.assertEqual(caught.exception.status, "auth_expired")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("loginUrl", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
