from __future__ import annotations

import base64
import hashlib
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.protocols import CanonicalRequest
from aibridge.providers import _parse_wenxin_stream, _stream_wenxin, _wenxin_token, parse_curl


def captured_token(prompt: str = "旧问题") -> str:
    lid = "7261897822204422614"
    digest = hashlib.md5(prompt.encode()).hexdigest()
    payload = base64.b64encode(f"seed1234|{digest}|1700000000000|{lid}".encode()).decode()
    return f"{payload}-{lid}-3"


def captured_body() -> dict:
    return {
        "message": {
            "inputMethod": "chat_search",
            "content": {"query": ""},
            "searchInfo": {
                "enter_type": "sidebar_dialog",
                "re_rank": "1",
                "sa": "bkb",
                "usedModel": {"modelName": "smartMode", "showModelName": "smartMode"},
                "chatParams": {"chat_samples": "WISE_NEW_CSAITAB", "chat_token": captured_token()},
            },
            "source": "pc_csaitab",
            "anti_ext": {"inputT": None, "ck1": 80, "ck9": 400, "ck10": 300},
            "query": [{"type": "TEXT", "data": {"text": {"query": "旧问题", "extData": "{}", "text_type": ""}}}],
        },
        "setype": "csaitab",
        "rank": 1,
    }


class WenxinHelpersTests(unittest.TestCase):
    def test_parse_curl_keeps_json_body(self) -> None:
        body = json.dumps(captured_body(), ensure_ascii=False, separators=(",", ":"))
        parsed = parse_curl(
            "curl 'https://chat.baidu.com/aichat/api/conversation' "
            "-H 'Cookie: BAIDUID=test' "
            f"--data-raw '{body}'"
        )

        self.assertEqual(parsed["request_url"], "https://chat.baidu.com/aichat/api/conversation")
        self.assertEqual(parsed["cookie"], "BAIDUID=test")
        self.assertEqual(json.loads(parsed["body"])["message"]["query"][0]["data"]["text"]["query"], "旧问题")

    def test_generates_fresh_prompt_bound_token(self) -> None:
        before = int(time.time() * 1000)
        token = _wenxin_token(captured_token(), "新问题")
        encoded, lid, version = token.rsplit("-", 2)
        seed, digest, timestamp, embedded_lid = base64.b64decode(encoded).decode().split("|")

        self.assertEqual(seed, "seed1234")
        self.assertEqual(digest, hashlib.md5("新问题".encode()).hexdigest())
        self.assertGreaterEqual(int(timestamp), before)
        self.assertEqual((lid, embedded_lid, version), ("7261897822204422614", "7261897822204422614", "3"))

    def test_parses_only_answer_generator(self) -> None:
        raw = "\n\n".join([
            'event:message\ndata:{"status":0,"data":{"message":{"content":{"generator":{"component":"markdown-yiyan","data":{"value":"你"}}}}}}',
            'event:message\ndata:{"status":0,"data":{"message":{"content":{"generator":{"component":"questionClosely","data":{"value":"忽略"}}}}}}',
            'event:message\ndata:{"status":0,"data":{"message":{"content":{"generator":{"component":"markdown-yiyan","data":{"value":"好"}}}}}}',
        ])
        self.assertEqual(_parse_wenxin_stream(raw), (["你", "好"], None, True))


class WenxinStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_rebuilds_token_header_and_prompt(self) -> None:
        raw = "\n\n".join([
            'event:message\ndata:{"status":0,"data":{"message":{"content":{"generator":{"component":"markdown-yiyan","data":{"value":"你"}}}}}}',
            'event:message\ndata:{"status":0,"data":{"message":{"content":{"generator":{"component":"markdown-yiyan","data":{"value":"好"}}}}}}',
        ])
        source = {
            "base_url": "https://wenxin.baidu.com",
            "credential": {
                "request_url": "https://chat.baidu.com/aichat/api/conversation",
                "headers": {"X-Chat-Message": "stale", "origin": "https://wenxin.baidu.com"},
                "cookie": "BAIDUID=test",
                "body": json.dumps(captured_body(), ensure_ascii=False),
            },
        }
        request = CanonicalRequest(model="wenxin-web", upstream_model="smartMode", messages=[{"role": "user", "content": "新问题"}])
        mocked_post = AsyncMock(return_value=(200, raw))

        with patch("aibridge.providers._curl_post", mocked_post):
            events = [event async for event in _stream_wenxin(source, request)]

        self.assertEqual("".join(event.text for event in events), "你好")
        url, headers, body = mocked_post.await_args.args
        self.assertEqual(url, "https://chat.baidu.com/aichat/api/conversation")
        self.assertEqual(headers["cookie"], "BAIDUID=test")
        self.assertNotEqual(headers["x-chat-message"], "stale")
        self.assertIn("%E6%96%B0%E9%97%AE%E9%A2%98", headers["x-chat-message"])
        self.assertIn("新问题", body["message"]["query"][0]["data"]["text"]["query"])
        self.assertNotEqual(body["message"]["searchInfo"]["chatParams"]["chat_token"], captured_token())


if __name__ == "__main__":
    unittest.main()
