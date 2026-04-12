"""
WebSocket connection manager for broadcasting state updates.
"""

from fastapi import WebSocket

from app.logging_utils import log_debug


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except TypeError as e:
                log_debug(f"broadcast serialize error (bug — check datetimes in snapshot): {e}")
                # Don't drop the WS on serialization errors — fix the data instead
            except Exception as e:
                log_debug(f"broadcast ws error: {type(e).__name__}: {e}")
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)
