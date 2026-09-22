from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any


logger = logging.getLogger(__name__)


class RelayError(RuntimeError):
    pass


@dataclass
class RelayClient:
    client_id: str
    websocket: Any
    source_ids: set[str]
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserRelay:
    def __init__(self) -> None:
        self._clients: dict[str, RelayClient] = {}
        self._pending: dict[str, tuple[asyncio.Future[dict[str, Any]], RelayClient]] = {}
        self._lock = asyncio.Lock()
        self._cursor = 0

    async def attach(self, client_id: str, websocket: Any, source_ids: set[str]) -> None:
        client = RelayClient(client_id, websocket, source_ids)
        async with self._lock:
            previous = self._clients.get(client_id)
            self._clients[client_id] = client
        if previous is not None:
            try:
                await previous.websocket.close(code=4001, reason="新的中继连接已建立")
            except Exception:
                pass
        try:
            await websocket.send_json({"type": "ready", "protocol_version": 2, "source_ids": sorted(source_ids)})
            logger.info("浏览器中继已连接：客户端=%s，来源=%s", client_id[:8], ",".join(sorted(source_ids)) or "无")
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                kind = message.get("type")
                if kind == "ping":
                    await websocket.send_json({"type": "pong"})
                elif kind == "result":
                    self.resolve(client, message)
        finally:
            async with self._lock:
                if self._clients.get(client_id) is client:
                    self._clients.pop(client_id, None)
            self._fail_client(client, "浏览器中继连接已断开")
            logger.info("浏览器中继已断开：客户端=%s", client_id[:8])

    def resolve(self, client: RelayClient, message: dict[str, Any]) -> None:
        task_id = str(message.get("task_id") or "")
        pending = self._pending.get(task_id)
        if not pending or pending[1] is not client:
            return
        future, _ = pending
        if not future.done():
            future.set_result(message)

    def _fail_client(self, client: RelayClient, reason: str) -> None:
        for task_id, (future, assigned_client) in list(self._pending.items()):
            if assigned_client is client and not future.done():
                future.set_exception(RelayError(reason))
                self._pending.pop(task_id, None)

    async def source_ids(self) -> list[str]:
        async with self._lock:
            return sorted({source_id for client in self._clients.values() for source_id in client.source_ids})

    async def request(self, source_id: str, action: str, payload: dict[str, Any], timeout: float = 190) -> dict[str, Any]:
        async with self._lock:
            candidates = [client for client in self._clients.values() if source_id in client.source_ids]
            if not candidates:
                raise RelayError("浏览器中继离线；请打开已配对的 Catcher 和 longcat.chat 页面")
            client = candidates[self._cursor % len(candidates)]
            self._cursor += 1

        task_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[task_id] = (future, client)
        try:
            logger.info("浏览器中继任务开始：任务=%s，来源=%s，动作=%s", task_id[:8], source_id, action)
            async with client.send_lock:
                await client.websocket.send_json({
                    "type": "task",
                    "task_id": task_id,
                    "source_id": source_id,
                    "action": action,
                    "payload": payload,
                })
            result = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise RelayError("浏览器中继执行超时") from exc
        except RelayError:
            raise
        except Exception as exc:
            raise RelayError(f"浏览器中继通信失败：{str(exc)[:160]}") from exc
        finally:
            self._pending.pop(task_id, None)

        if result.get("ok") is not True:
            raise RelayError(str(result.get("error") or "浏览器中继执行失败")[:500])
        response = result.get("response")
        if not isinstance(response, dict):
            raise RelayError("浏览器中继返回格式无效")
        logger.info(
            "浏览器中继任务完成：任务=%s，来源=%s，阶段=%s，HTTP=%s，响应字节=%s",
            task_id[:8],
            source_id,
            str(response.get("stage") or "未知")[:40],
            response.get("status"),
            len(str(response.get("body") or "").encode("utf-8")),
        )
        return response


relay_broker = BrowserRelay()
