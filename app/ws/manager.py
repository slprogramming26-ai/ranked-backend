from fastapi import WebSocket
from typing import Dict
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from .. import models, schemas


class ChatError(Exception):
    """Domain-Fehler aus dem Manager (Gruppe gibt's nicht, kein Mitglied, ...).

    Wir werfen einen normalen Python-Fehler statt HTTPException, weil HTTPException
    im WebSocket-Kontext nicht funktioniert (kein HTTP-Response möglich).
    Die Route fängt den hier, packt das in eine `kind: "error"`-Nachricht und
    schickt sie über den Socket zurück."""


class ConnectionManager:
    """Hält alle offenen WebSocket-Verbindungen und routet Nachrichten dazwischen.

    Datenstruktur: Dict[user_id, WebSocket]
      → Ein User kann immer nur EINE Verbindung gleichzeitig haben.
      → Verbindet ein User sich nochmal (zweiter Tab), überschreibt das die alte.
      → Multi-Device wäre `Dict[int, List[WebSocket]]` — bewusst nicht jetzt,
        weil Flutter erstmal nur eine Verbindung pro User aufmacht.

    WICHTIG: Dieser Manager ist In-Memory. Ein zweiter Server-Replica würde
    seine eigene Map haben — User auf Replica A könnten User auf Replica B
    nicht erreichen. Skalierung später via Redis Pub/Sub, jetzt erstmal egal."""

    def __init__(self):
        self.active_connections: Dict[int, WebSocket] = {}


    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int):
        self.active_connections.pop(user_id, None)


    async def send_to_user(
        self,
        sender_id: int,
        recipient_id: int,
        content: str,
        db: Session,
    ) -> int:
        """DM an einen User schicken.

        Return: 1 wenn live ausgeliefert, 0 wenn der Empfänger offline war
                und die Nachricht in der pending-Queue (DB) gelandet ist.
        Der Sender erfährt das später in der ack-Nachricht."""

        socket = self.active_connections.get(recipient_id)
        
        #offline
        if socket is None:
            db.add(models.Message(
                sender_id=sender_id,
                recipient_id=recipient_id,
                message=content,
            ))
            db.commit()
            return 0

        #online
        out = schemas.ChatMessageOut(
            sender_id=sender_id,
            message=content,
            created_at=datetime.now(timezone.utc),
        )
        # mode="json" → datetime wird als ISO-String serialisiert,
        # nicht als Python-datetime-Objekt (das wäre nicht JSON-fähig).
        await socket.send_json(out.model_dump(mode="json"))
        return 1

    # Group Messages
    

    async def send_to_group(
        self,
        sender_id: int,
        group_chat_id: int,
        content: str,
        db: Session,
    ) -> int:
        """Gruppen-Nachricht an alle Mitglieder (außer Sender) verteilen.

        Return: Anzahl der LIVE ausgelieferten Empfänger.
                Der Rest wurde in der pending-Queue gespeichert.

        Raised ChatError bei: Gruppe existiert nicht, Sender ist kein Mitglied."""

        group = db.query(models.GroupChats).filter(
            models.GroupChats.group_chat_id == group_chat_id,
        ).first()
        if group is None:
            raise ChatError(f"group_chat {group_chat_id} not found")

        # 2) Darf der Sender überhaupt in dieser Gruppe schreiben?
        is_member = db.query(models.GroupChatMembership).filter(
            models.GroupChatMembership.group_chat_id == group_chat_id,
            models.GroupChatMembership.participant_id == sender_id,
        ).first()
        if is_member is None:
            raise ChatError("not a member of this group")

        
        members = db.query(models.GroupChatMembership).filter(
            models.GroupChatMembership.group_chat_id == group_chat_id,
            models.GroupChatMembership.participant_id != sender_id,
        ).all()

        
        timestamp = datetime.now(timezone.utc)
        out = schemas.GroupChatMessageOut(
            group_chat_id=group_chat_id,
            sender_id=sender_id,
            message=content,
            created_at=timestamp,
        )
        payload = out.model_dump(mode="json")

        delivered_live = 0
        pending = []  # alles was offline ist: erst sammeln, dann einziges add_all+commit

        for member in members:
            socket = self.active_connections.get(member.participant_id)
            if socket is None:
                pending.append(models.GroupMessage(
                    group_chat_id=group_chat_id,
                    sender_id=sender_id,
                    recipient_id=member.participant_id,
                    message=content,
                ))
            else:
                await socket.send_json(payload)
                delivered_live += 1

        
        if pending:
            db.add_all(pending)
            db.commit()

        return delivered_live


    async def flush_pending(self, user_id: int, db: Session):
        

        socket = self.active_connections.get(user_id)
        if socket is None:
            return

        pending_dms = (
            db.query(models.Message)
            .filter(models.Message.recipient_id == user_id)
            .order_by(models.Message.created_at)
            .all()
        )
        for msg in pending_dms:
            out = schemas.ChatMessageOut(
                sender_id=msg.sender_id,
                message=msg.message,
                created_at=msg.created_at,
            )
            await socket.send_json(out.model_dump(mode="json"))
            db.delete(msg)

        
        pending_groups = (
            db.query(models.GroupMessage)
            .filter(models.GroupMessage.recipient_id == user_id)
            .order_by(models.GroupMessage.created_at)
            .all()
        )
        for msg in pending_groups:
            out = schemas.GroupChatMessageOut(
                group_chat_id=msg.group_chat_id,
                sender_id=msg.sender_id,
                message=msg.message,
                created_at=msg.created_at,
            )
            await socket.send_json(out.model_dump(mode="json"))
            db.delete(msg)

        
        db.commit()


manager = ConnectionManager()
