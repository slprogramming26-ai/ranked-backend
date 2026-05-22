from fastapi import WebSocket
from typing import List


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []  # Liste aller Verbundenen

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)  # In Liste eintragen

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)  # Aus Liste austragen

    async def send_to_everyone(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)    # An alle schicken


manager = ConnectionManager()
