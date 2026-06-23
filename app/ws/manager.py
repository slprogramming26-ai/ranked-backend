from fastapi import WebSocket
from typing import Dict
from sqlalchemy.orm import Session
from .. import models, schemas
from fastapi.concurrency import run_in_threadpool


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

    def blocked_user_ids(self, user_id: int, db: Session) -> set:
        """Alle User-IDs, mit denen `user_id` eine Block-Beziehung hat —
        egal ob user_id selbst blockiert hat oder blockiert wurde.
        Frisch aus der DB, kein Cache → wirkt sofort, auch wenn der Block
        erst während der laufenden Verbindung passiert. Für den Group-Fanout:
        einmal laden, dann gegen die Mitgliederliste filtern."""
        blocks = db.query(models.Block).filter(
            (models.Block.blocker_id == user_id) | (models.Block.blocked_id == user_id)
        ).all()
        ids = set()
        for b in blocks:
            ids.add(b.blocked_id if b.blocker_id == user_id else b.blocker_id)
        return ids

    def is_blocked(self, sender_id: int, recipient_id: int, db: Session) -> bool:
        """True, wenn zwischen den beiden eine Block-Beziehung besteht
        (in beide Richtungen). Direkt aus der DB → immer aktuell."""
        block = db.query(models.Block).filter(
            ((models.Block.blocker_id == sender_id) & (models.Block.blocked_id == recipient_id))
            | ((models.Block.blocker_id == recipient_id) & (models.Block.blocked_id == sender_id))
        ).first()
        return block is not None
    



    async def accept(self, websocket: WebSocket):
        await websocket.accept()

    def register(self, user_id: int, websocket: WebSocket):
        """Merkt sich den aktiven Socket des Users für Live-Pushes.
        Verpasstes holt der Client selbst per REST (GET /messages?since=…),
        deshalb ist hier keine Flush-/Reihenfolge-Logik mehr nötig."""
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int):
        self.active_connections.pop(user_id, None)


    def _prepare_dm_send(self,
        sender_id: int,
        recipient_id: int,
        content: str,
        db: Session,
        client_msg_id: str | None = None,):


        if self.is_blocked(sender_id, recipient_id, db):
            raise ChatError("Nachricht kann nicht gesendet werden. Ein Nutzer hat den anderen blockiert.")

        # 1) IMMER zuerst speichern — der Server ist jetzt die Quelle der Wahrheit.
        #    Der Empfänger holt sich Verpasstes später per REST ("seit Zeitstempel X").
        #    refresh() lädt das von der DB gesetzte created_at zurück, damit die
        #    live gepushte Zeit exakt der gespeicherten entspricht.
        message = models.Message(
            sender_id=sender_id,
            recipient_id=recipient_id,
            message=content,
            client_msg_id=client_msg_id,
        )
        db.add(message)
        db.commit()
        db.refresh(message)

        # Nur einfache Daten zurückgeben (kein ORM-Objekt) -> keine Lazy-Load-Falle.
        return message.created_at


    async def send_to_user(
        self,
        sender_id: int,
        recipient_id: int,
        content: str,
        db: Session,
        client_msg_id: str | None = None,
    ) -> int:

        # Phase 1: DB-Arbeit im Threadpool (ein Hop), gibt nur created_at zurück.
        created_at = await run_in_threadpool(
            self._prepare_dm_send,
            sender_id,
            recipient_id,
            content,
            db,
            client_msg_id,
        )

        # Phase 2: der Live-Send MUSS auf dem Event-Loop bleiben (echtes async).
        socket = self.active_connections.get(recipient_id)
        if socket is not None:
            out = schemas.ChatMessageOut(
                sender_id=sender_id,
                message=content,
                created_at=created_at,
                client_msg_id=client_msg_id,
            )
            # mode="json" → datetime wird als ISO-String serialisiert,
            # nicht als Python-datetime-Objekt (das wäre nicht JSON-fähig).
            if await _safe_send(socket, out.model_dump(mode="json")):
                return 1
            # Send fehlgeschlagen → Socket ist tot, raus aus der Map.
            # Die Nachricht ist schon gespeichert, der Client holt sie beim Reconnect.
            self.active_connections.pop(recipient_id, None)

        return 0

    # Group Messages

    def _prepare_group_send(
            self,
        sender_id: int,
        group_chat_id: int,
        content: str,
        db: Session,
        client_msg_id: str | None = None,
    ) -> tuple:
        group = db.query(models.GroupChats).filter(
            models.GroupChats.group_chat_id == group_chat_id,
        ).first()
        if group is None:
            raise ChatError(f"group_chat {group_chat_id} not found")

        is_member = db.query(models.GroupChatMembership).filter(
            models.GroupChatMembership.group_chat_id == group_chat_id,
            models.GroupChatMembership.participant_id == sender_id,
        ).first()
        if is_member is None:
            raise ChatError("not a member of this group")

        message = models.GroupMessage(
            group_chat_id=group_chat_id,
            sender_id=sender_id,
            message=content,
            client_msg_id=client_msg_id,
        )
        db.add(message)
        db.commit()
        db.refresh(message)

        # 2) Live an alle ONLINE-Mitglieder (außer Sender, außer geblockte) pushen.
        members = db.query(models.GroupChatMembership).filter(
            models.GroupChatMembership.group_chat_id == group_chat_id,
            models.GroupChatMembership.participant_id != sender_id,
        ).all()

        # Geblockte rausfiltern: einmal die Block-Liste des Senders laden, dann filtern.
        blocked = self.blocked_user_ids(sender_id, db)
        members = [m for m in members if m.participant_id not in blocked]

        

        return message.created_at, [m.participant_id for m in members]




    async def send_to_group(
        self,
        sender_id: int,
        group_chat_id: int,
        content: str,
        db: Session,
        client_msg_id: str | None = None,
    ) -> int:
        """Gruppen-Nachricht speichern und an alle ONLINE-Mitglieder (außer Sender) live verteilen.

        Return: Anzahl der LIVE ausgelieferten Empfänger. Offline-Mitglieder holen sich
                die Nachricht später per REST ("seit Zeitstempel X").

        Raised ChatError bei: Gruppe existiert nicht, Sender ist kein Mitglied."""

        # Phase 1: die komplette DB-Arbeit im Threadpool (ein Hop), gibt nur einfache Daten zurück.
        created_at, recipient_ids = await run_in_threadpool(
            self._prepare_group_send,
            sender_id,
            group_chat_id,
            content,
            db,
            client_msg_id,
        )

        # Phase 2: die Live-Sends MÜSSEN auf dem Event-Loop bleiben (echtes async).
        out = schemas.GroupChatMessageOut(
            group_chat_id=group_chat_id,
            sender_id=sender_id,
            message=content,
            created_at=created_at,
            client_msg_id=client_msg_id,
        )
        payload = out.model_dump(mode="json")

        delivered_live = 0
        dead_sockets = []   # User deren Socket beim Send gestorben ist → aus Map raus

        # recipient_ids ist eine Liste von participant_ids (ints), keine ORM-Objekte.
        for uid in recipient_ids:
            socket = self.active_connections.get(uid)
            if socket is None:
                continue  # offline → holt sich die Nachricht später per REST
            if await _safe_send(socket, payload):
                delivered_live += 1
            else:
                # Socket kaputt → später aus Map räumen. Nachricht ist schon gespeichert.
                dead_sockets.append(uid)

        for uid in dead_sockets:
            self.active_connections.pop(uid, None)

        return delivered_live


manager = ConnectionManager()
