from __future__ import annotations

import asyncio
import contextlib
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from aibridge.relay import BrowserRelay, RelayError


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.received: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        self.closed = False

    async def send_json(self, message: dict[str, Any]) -> None:
        await self.sent.put(message)

    async def receive_json(self) -> dict[str, Any]:
        message = await self.received.get()
        if isinstance(message, BaseException):
            raise message
        return message

    async def close(self, code: int, reason: str) -> None:
        self.closed = True

    async def next_sent(self, kind: str) -> dict[str, Any]:
        while True:
            message = await asyncio.wait_for(self.sent.get(), timeout=1)
            if message.get("type") == kind:
                return message

    async def reply(self, message: dict[str, Any]) -> None:
        await self.received.put(message)

    async def disconnect(self) -> None:
        await self.received.put(ConnectionError("连接关闭"))


class BrowserRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task() and task.get_name().startswith("relay-test-"):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await task

    async def attach(self, relay: BrowserRelay, client_id: str, websocket: FakeWebSocket) -> asyncio.Task[None]:
        task = asyncio.create_task(
            relay.attach(client_id, websocket, {"web-longcat"}),
            name=f"relay-test-{client_id}-{id(websocket)}",
        )
        await websocket.next_sent("ready")
        return task

    async def test_request_dispatches_task_and_returns_response(self) -> None:
        relay = BrowserRelay()
        websocket = FakeWebSocket()
        await self.attach(relay, "client-1", websocket)

        request = asyncio.create_task(
            relay.request("web-longcat", "chat", {"prompt": "你好"}, timeout=1)
        )
        task_message = await websocket.next_sent("task")
        await websocket.reply({
            "type": "result",
            "task_id": task_message["task_id"],
            "ok": True,
            "response": {"status": 200, "body": "ok"},
        })

        self.assertEqual(await request, {"status": 200, "body": "ok"})

    async def test_request_reports_offline_without_matching_client(self) -> None:
        relay = BrowserRelay()

        with self.assertRaisesRegex(RelayError, "中继离线"):
            await relay.request("web-longcat", "chat", {}, timeout=1)

    async def test_disconnect_fails_assigned_task(self) -> None:
        relay = BrowserRelay()
        websocket = FakeWebSocket()
        attached = await self.attach(relay, "client-1", websocket)
        request = asyncio.create_task(relay.request("web-longcat", "chat", {}, timeout=1))
        await websocket.next_sent("task")

        await websocket.disconnect()
        with self.assertRaises(ConnectionError):
            await attached
        with self.assertRaisesRegex(RelayError, "连接已断开"):
            await request

    async def test_replaced_connection_cannot_fail_new_connection_task(self) -> None:
        relay = BrowserRelay()
        old_websocket = FakeWebSocket()
        old_attached = await self.attach(relay, "client-1", old_websocket)

        new_websocket = FakeWebSocket()
        await self.attach(relay, "client-1", new_websocket)
        self.assertTrue(old_websocket.closed)

        request = asyncio.create_task(relay.request("web-longcat", "chat", {}, timeout=1))
        task_message = await new_websocket.next_sent("task")
        await old_websocket.disconnect()
        with self.assertRaises(ConnectionError):
            await old_attached
        await new_websocket.reply({
            "type": "result",
            "task_id": task_message["task_id"],
            "ok": True,
            "response": {"status": 200},
        })

        self.assertEqual(await request, {"status": 200})


if __name__ == "__main__":
    unittest.main()
