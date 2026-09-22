from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.catcher import build_capture_credential
from aibridge.storage import Storage


class CatcherCaptureTests(unittest.TestCase):
    def test_deepseek_capture_normalizes_headers_and_cookie(self) -> None:
        source_id, credential = build_capture_credential({
            "source_id": "web-deepseek",
            "url": "https://chat.deepseek.com/api/v0/chat/completion",
            "method": "POST",
            "headers": [
                {"name": "Authorization", "value": "Bearer token"},
                {"name": "Cookie", "value": "session=abc"},
                {"name": "Content-Length", "value": "999"},
                {"name": "X-Ds-Pow-Response", "value": "old"},
            ],
            "body": {"text": json.dumps({"prompt": "你好"}, ensure_ascii=False)},
        })
        self.assertEqual(source_id, "web-deepseek")
        self.assertEqual(credential["cookie"], "session=abc")
        self.assertEqual(credential["headers"]["Authorization"], "Bearer token")
        self.assertNotIn("Content-Length", credential["headers"])
        self.assertIn("你好", credential["body"])

    def test_kimi_storage_tokens_override_short_lived_header(self) -> None:
        _, credential = build_capture_credential({
            "source_id": "web-kimi",
            "url": "https://www.kimi.com/apiv2/kimi.gateway.chat.v1.ChatService/Chat",
            "method": "POST",
            "headers": {"authorization": "Bearer old"},
            "page_storage": {"access_token": "new-access", "refresh_token": "new-refresh"},
        })
        self.assertEqual(credential["headers"]["authorization"], "Bearer new-access")
        self.assertEqual(credential["refresh_token"], "new-refresh")

    def test_wenxin_capture_accepts_cross_origin_chat_host(self) -> None:
        source_id, credential = build_capture_credential({
            "source_id": "web-wenxin",
            "url": "https://chat.baidu.com/aichat/api/conversation",
            "method": "POST",
            "headers": {"content-type": "application/json"},
            "cookie": "BAIDUID=test",
            "body": {"text": '{"message":{"query":[]}}'},
        })
        self.assertEqual(source_id, "web-wenxin")
        self.assertEqual(credential["request_url"], "https://chat.baidu.com/aichat/api/conversation")
        self.assertEqual(credential["cookie"], "BAIDUID=test")

    def test_qwen_capture_keeps_required_browser_headers(self) -> None:
        _, credential = build_capture_credential({
            "source_id": "web-qwen",
            "url": "https://chat.qwen.ai/api/v2/chat/completions?chat_id=test",
            "method": "POST",
            "headers": {
                "$bx-ua": "signed-browser-data",
                "bx-umidtoken": "umid",
                "bx-v": "2.5.37",
                "source": "web",
                "Timezone": "Mon Sep 21 2026 13:20:39 GMT+0800",
                "Version": "0.2.91",
            },
            "body": {"text": '{"stream":true}'},
        })
        self.assertEqual(credential["headers"]["Version"], "0.2.91")
        self.assertEqual(credential["headers"]["$bx-ua"], "signed-browser-data")
        self.assertEqual(credential["body"], '{"stream":true}')

    def test_longcat_capture_enables_relay_without_saving_ephemeral_signature(self) -> None:
        source_id, credential = build_capture_credential({
            "source_id": "web-longcat",
            "url": "https://longcat.chat/api/v1/chat-completion-V2?yodaReady=h5",
            "method": "POST",
            "headers": {
                "m-appkey": "stable-app-key",
                "m-traceid": "ephemeral-trace",
                "mtgsig": "ephemeral-signature",
                "x-client-language": "zh-CN",
            },
            "body": {"text": '{"conversationId":"old","content":"old"}'},
        })
        self.assertEqual(source_id, "web-longcat")
        self.assertTrue(credential["browser_relay"])
        self.assertEqual(credential["headers"]["m-appkey"], "stable-app-key")
        normalized = {key.lower(): value for key, value in credential["headers"].items()}
        self.assertNotIn("m-traceid", normalized)
        self.assertNotIn("mtgsig", normalized)
        self.assertEqual(credential["body"], "")

    def test_capture_rejects_wrong_host(self) -> None:
        with self.assertRaisesRegex(ValueError, "域名"):
            build_capture_credential({
                "source_id": "web-deepseek",
                "url": "https://example.com/api/v0/chat/completion",
                "method": "POST",
            })


class CatcherStorageTests(unittest.TestCase):
    def test_existing_install_adds_longcat_once_without_overriding_later_choice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "aibridge.db"
            secret = root / "secret.key"
            Storage(database, secret)
            old_sources = [
                "web-deepseek", "web-kimi", "web-qwen", "web-doubao",
                "web-perplexity", "web-wenxin",
            ]
            db = sqlite3.connect(database)
            try:
                db.execute(
                    "UPDATE catcher_settings SET enabled=1,auto_enable=1,allowed_sources_json=? WHERE id=1",
                    (json.dumps(old_sources),),
                )
                db.execute("DELETE FROM app_migrations")
                db.commit()
            finally:
                db.close()

            migrated = Storage(database, secret)
            self.assertIn("web-longcat", migrated.catcher_state()["allowed_source_ids"])

            migrated.update_catcher_settings(
                enabled=True,
                auto_enable=True,
                allowed_source_ids=old_sources,
            )
            reopened = Storage(database, secret)
            self.assertNotIn("web-longcat", reopened.catcher_state()["allowed_source_ids"])

    def test_pairing_is_single_use_and_client_token_can_be_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "aibridge.db", root / "secret.key")
            initial = storage.catcher_state()
            storage.update_catcher_settings(
                enabled=True,
                auto_enable=False,
                allowed_source_ids=initial["allowed_source_ids"],
            )
            code, _ = storage.create_catcher_pairing()
            paired = storage.consume_catcher_pairing(code, "测试扩展")
            self.assertIsNotNone(paired)
            client, token = paired or ({}, "")
            self.assertEqual(client["name"], "测试扩展")
            self.assertEqual(storage.verify_catcher_client(token)["id"], client["id"])
            self.assertIsNone(storage.consume_catcher_pairing(code, "第二个扩展"))


if __name__ == "__main__":
    unittest.main()
