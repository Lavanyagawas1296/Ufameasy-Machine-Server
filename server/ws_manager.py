from fastapi import WebSocket
from typing import Dict
import asyncio, json

# Per-client outbox: one slow/half-dead client (e.g. a phone on flaky Wi-Fi)
# only backs up its own queue and never delays the others.
SEND_TIMEOUT_S = 5.0
MAX_QUEUED = 256

class ConnectionManager:
    def __init__(self):
        self.active: Dict[WebSocket, asyncio.Queue] = {}
        self._senders: Dict[WebSocket, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        queue = asyncio.Queue(maxsize=MAX_QUEUED)
        async with self._lock:
            self.active[ws] = queue
            self._senders[ws] = asyncio.create_task(self._sender(ws, queue))

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.active.pop(ws, None)
            task = self._senders.pop(ws, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    async def _sender(self, ws: WebSocket, queue: asyncio.Queue):
        # Sends in FIFO order, so per-client message ordering is preserved.
        try:
            while True:
                message = await queue.get()
                await asyncio.wait_for(ws.send_text(message), SEND_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._drop(ws)

    async def _drop(self, ws: WebSocket):
        await self.disconnect(ws)
        try:
            await asyncio.wait_for(ws.close(), SEND_TIMEOUT_S)
        except Exception:
            pass

    async def broadcast(self, data: dict):
        message = json.dumps(data)
        async with self._lock:
            targets = list(self.active.items())

        for ws, queue in targets:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Client has fallen too far behind; drop it rather than
                # buffer without bound. It will reconnect and get a fresh init.
                asyncio.create_task(self._drop(ws))

manager = ConnectionManager()
