from fastapi import WebSocket
from typing import Set
import asyncio, json

class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.active.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.active.discard(ws)

    async def broadcast(self, data: dict):
        message = json.dumps(data)
        async with self._lock:
            targets = list(self.active)

        dead = set()
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)

        if dead:
            async with self._lock:
                self.active -= dead

manager = ConnectionManager()
