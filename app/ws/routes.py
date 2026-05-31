from contextlib import contextmanager
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from sqlalchemy.orm import Session
from jose import JWTError, jwt
from pydantic import ValidationError
from .. import models, schemas, oauth2
from ..database import SessionLocal
from .manager import manager, ChatError


router = APIRouter()


@contextmanager
def _db_scope():
    """Kurzlebige DB-Session pro Nachricht/Operation. Wir wollen NICHT EINE Session
    für die ganze Socket-Lebenszeit halten — das blockt einen Pool-Slot dauerhaft
    und der Transaction-State driftet ab (stale reads bei parallelen Writes)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_user_from_token(token: str, db: Session) -> models.User | None:
    try:
        payload = jwt.decode(token, oauth2.SECRET_KEY, algorithms=[oauth2.ALGORITHM])
        user_id = payload.get("user_id")
        if user_id is None:
            return None
    except JWTError:
        return None
    return db.query(models.User).filter(models.User.id == user_id).first()




@router.websocket("/ws/chat")
async def chat(
    websocket: WebSocket,
    token: str = Query(...),
):
    with _db_scope() as db:
        user = get_user_from_token(token, db)
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.accept(websocket)

    # WICHTIG: flush BEVOR wir in active_connections registrieren — sonst kann
    # eine Live-Nachricht von einem anderen User dazwischenrutschen und der Client
    # sieht "neue" Nachrichten vor älteren pending-Messages.
    with _db_scope() as db:

        manager.load_block_user(user.id, db)
        
        flushed_ok = await manager.flush_pending(user.id, websocket, db)
    if not flushed_ok:
        # Socket starb mitten im Flush → gar nicht erst registrieren.
        return

    manager.register(user.id, websocket)

    try:
        while True:
            raw = await websocket.receive_json()
            kind = raw.get("kind") if isinstance(raw, dict) else None

            if kind == "dm":
                await _handle_dm(websocket, user, raw)
            elif kind == "group":
                await _handle_group(websocket, user, raw)
            else:
                await websocket.send_json({
                    "kind": "error",
                    "detail": "missing or unknown 'kind' (expected 'dm' or 'group')",
                })

    except WebSocketDisconnect:
        pass
    except Exception:
        # Alles andere (DB-Fehler, unerwartete Exceptions) darf den Socket nicht
        # halb-tot im Manager hinterlassen — wir räumen im finally auf.
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        manager.disconnect(user.id)



async def _handle_dm(websocket: WebSocket, user: models.User, raw: dict):
    try:
        incoming = schemas.ChatMessageIn.model_validate(raw)
    except ValidationError as e:
        await websocket.send_json({
            "kind": "error",
            "detail": e.errors(include_url=False, include_context=False),
        })
        return

    with _db_scope() as db:
        

        delivered_live = await manager.send_to_user(
            sender_id=user.id,
            recipient_id=incoming.to,
            content=incoming.message,
            db=db,
        )

    ack = schemas.ChatAck(to=incoming.to, delivered_live=delivered_live)
    await websocket.send_json(ack.model_dump(mode="json"))


async def _handle_group(websocket: WebSocket, user: models.User, raw: dict):

    
    try:
        incoming = schemas.GroupChatMessageIn.model_validate(raw)
    except ValidationError as e:
        await websocket.send_json({
            "kind": "error",
            "detail": e.errors(include_url=False, include_context=False),
        })
        return

    try:
        with _db_scope() as db:
            delivered_live = await manager.send_to_group(
                sender_id=user.id,
                group_chat_id=incoming.to,
                content=incoming.message,
                db=db,
            )
    except ChatError as e:
        await websocket.send_json({"kind": "error", "detail": str(e)})
        return

    ack = schemas.ChatAck(to=incoming.to, delivered_live=delivered_live)
    await websocket.send_json(ack.model_dump(mode="json"))
