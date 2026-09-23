from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.app import ClosingStreamingResponse


class ClosingStreamingResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_failure_closes_body_iterator(self) -> None:
        closed = asyncio.Event()

        async def body():
            try:
                yield b"first"
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        response = ClosingStreamingResponse(body())
        with self.assertRaises(OSError):
            await response.stream_response(send)
        self.assertTrue(closed.is_set())


if __name__ == "__main__":
    unittest.main()
