from __future__ import annotations

import base64
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.providers import refresh_kimi_credential


def jwt(expiry: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


class FakeResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json() -> dict[str, str]:
        return {"access_token": "fresh-access", "refresh_token": "fresh-refresh"}


class FakeClient:
    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, str]) -> FakeResponse:
        assert url.endswith("account.gateway.v1.AuthService/RefreshToken")
        assert headers["content-type"] == "application/json"
        assert json == {"refresh_token": "old-refresh"}
        return FakeResponse()


class KimiRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_access_token_is_refreshed_and_rotated(self) -> None:
        source = {
            "protocol": "kimi_web",
            "credential": {
                "request_url": "https://www.kimi.com/apiv2/kimi.gateway.chat.v1.ChatService/Chat",
                "headers": {"Authorization": f"Bearer {jwt(int(time.time()) - 1)}"},
                "refresh_token": "old-refresh",
            },
        }
        with patch("aibridge.providers.httpx.AsyncClient", return_value=FakeClient()):
            changed = await refresh_kimi_credential(source)
        self.assertTrue(changed)
        self.assertEqual(source["credential"]["headers"]["Authorization"], "Bearer fresh-access")
        self.assertEqual(source["credential"]["refresh_token"], "fresh-refresh")

    async def test_fresh_access_token_does_not_refresh(self) -> None:
        source = {
            "protocol": "kimi_web",
            "credential": {
                "headers": {"Authorization": f"Bearer {jwt(int(time.time()) + 3600)}"},
                "refresh_token": "refresh",
            },
        }
        changed = await refresh_kimi_credential(source)
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
