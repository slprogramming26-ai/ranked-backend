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


async def _safe_send(socket: WebSocket, payload: dict) -> bool:
    """Send über einen Socket der vielleicht schon tot ist.
    True = raus, False = Socket kaputt (Caller behandelt das wie offline)."""
    try:
        await socket.send_json(payload)
        return True
    except Exception:
        return False


class ConnectionManager:
    
    def __init__(self):
        self.active_connections: Dict[int, WebSocket] = {}

        self.block_cache: Dict[int, set] = {}

    def load_block_user(self, user_id: int, db: Session):

        blocks = db.query(models.Block).filter(
            (models.Block.blocker_id == user_id) | (models.Block.blocked_id == user_id)
        ).all()

        blocked_ids = set()

        for b in blocks:
            # Wir speichern die ID des "anderen" in das Set
            if b.blocker_id == user_id:
                blocked_ids.add(b.blocked_id)
            else:
                blocked_ids.add(b.blocker_id)
                
        self.block_cache[user_id] = blocked_ids

    def is_blocked(self, sender_id: int, recipient_id: int) -> bool:

        sender_blocks = self.block_cache.get(sender_id, set())
        return recipient_id in sender_blocks


    async def accept(self, websocket: WebSocket):
        await websocket.accept()

    def register(self, user_id: int, websocket: WebSocket):
        """Erst NACH flush_pending aufrufen, sonst kann eine Live-Nachricht
        zwischen den nachgereichten pending-Messages eintreffen → Out-of-Order."""
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int):
        self.active_connections.pop(user_id, None)
        self.block_cache.pop(user_id, None)


    async def send_to_user(
        self,
        sender_id: int,
        recipient_id: int,
        content: str,
        db: Session,
    ) -> int:

        
        if self.is_blocked(sender_id, recipient_id):
            raise ChatError("Nachricht kann nicht gesendet werden. Ein Nutzer hat den anderen blockiert.")

        socket = self.active_connections.get(recipient_id)

        if socket is not None:
            out = schemas.ChatMessageOut(
                sender_id=sender_id,
                message=content,
                created_at=datetime.now(timezone.utc),
            )
            # mode="json" → datetime wird als ISO-String serialisiert,
            # nicht als Python-datetime-Objekt (das wäre nicht JSON-fähig).
            if await _safe_send(socket, out.model_dump(mode="json")):
                return 1
            # Send fehlgeschlagen → Socket ist tot, raus aus der Map und queuen.
            self.active_connections.pop(recipient_id, None)

        db.add(models.Message(
            sender_id=sender_id,
            recipient_id=recipient_id,
            message=content,
        ))
        db.commit()
        return 0

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
        pending = []          # alles was offline ist: erst sammeln, dann einziges add_all+commit
        dead_sockets = []     # User deren Socket beim Send gestorben ist → aus Map raus

        for member in members:
            socket = self.active_connections.get(member.participant_id)
            if socket is None:
                pending.append(models.GroupMessage(
                    group_chat_id=group_chat_id,
                    sender_id=sender_id,
                    recipient_id=member.participant_id,
                    message=content,
                ))
                continue

            if await _safe_send(socket, payload):
                delivered_live += 1
            else:
                # Socket kaputt → wie offline behandeln (queuen) und später aus Map räumen.
                dead_sockets.append(member.participant_id)
                pending.append(models.GroupMessage(
                    group_chat_id=group_chat_id,
                    sender_id=sender_id,
                    recipient_id=member.participant_id,
                    message=content,
                ))


        if pending:
            db.add_all(pending)
            db.commit()

        for uid in dead_sockets:
            self.active_connections.pop(uid, None)

        return delivered_live


    async def flush_pending(self, user_id: int, socket: WebSocket, db: Session) -> bool:
        """Pending-Queue nach Reconnect leeren. Commit pro Nachricht, damit ein
        Socket-Crash mitten im Flush nur die schon zugestellten löscht, nicht den Rest.

        Return: True wenn alles ausgeliefert, False wenn der Socket dabei gestorben ist."""

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
            if not await _safe_send(socket, out.model_dump(mode="json")):
                return False
            db.delete(msg)
            db.commit()


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
            if not await _safe_send(socket, out.model_dump(mode="json")):
                return False
            db.delete(msg)
            db.commit()

        return True


manager = ConnectionManager()
