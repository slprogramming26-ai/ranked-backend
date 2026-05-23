from fastapi import WebSocket
from typing import Dict
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from .. import models, schemas


class ConnectionManager:
    def __init__(self):
        # user_id → der WebSocket dieses Users
        self.active_connections: Dict[int, WebSocket] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        # Zweiter Tab? Alte Verbindung wird überschrieben.
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int):
        self.active_connections.pop(user_id, None)

    async def send_to_user(
        self,
        sender_id: int,
        recipient_id: int,
        content: str,
        db: Session,
    ) -> bool:
        """Live an Empfänger schicken wenn online, sonst in DB parken.
        Return: True = live ausgeliefert, False = gequeued."""
        socket = self.active_connections.get(recipient_id)
        if socket is None:
            # Empfänger offline → kurz in DB zwischenspeichern
            pending = models.Message(
                sender_id=sender_id,
                recipient_id=recipient_id,
                message=content,
            )
            db.add(pending)
            db.commit()
            return False

        # Empfänger online → direkt durchreichen
        out = schemas.ChatMessageOut(
            sender_id=sender_id,
            message=content,
            created_at=datetime.now(timezone.utc),
        )
        await socket.send_json(out.model_dump(mode="json"))
        return True

    async def flush_pending(self, user_id: int, db: Session):
        """Alle gespeicherten Nachrichten für diesen User ausliefern
        und aus der DB löschen."""
        socket = self.active_connections.get(user_id)
        if socket is None:
            return

        pending = (
            db.query(models.Message)
            .filter(models.Message.recipient_id == user_id)
            .order_by(models.Message.created_at)
            .all()
        )

        for msg in pending:
            out = schemas.ChatMessageOut(
                sender_id=msg.sender_id,
                message=msg.message,
                created_at=msg.created_at,
            )
            await socket.send_json(out.model_dump(mode="json"))
            db.delete(msg)
        db.commit()


manager = ConnectionManager()
