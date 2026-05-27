from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query, status, HTTPException
from sqlalchemy.orm import Session
from jose import JWTError, jwt
from pydantic import ValidationError
from .. import models, schemas, oauth2
from ..database import get_dp
from .manager import manager, ChatError


router = APIRouter()




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
    db: Session = Depends(get_dp),
):
    
    user = get_user_from_token(token, db)
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    
    await manager.connect(user.id, websocket)

   
    await manager.flush_pending(user.id, db)

    try:
        while True:
            
            raw = await websocket.receive_json()
            kind = raw.get("kind") if isinstance(raw, dict) else None

            
            if kind == "dm":
                await _handle_dm(websocket, user, raw, db)
            elif kind == "group":
                await _handle_group(websocket, user, raw, db)
            else:
                await websocket.send_json({
                    "kind": "error",
                    "detail": "missing or unknown 'kind' (expected 'dm' or 'group')",
                })

    except WebSocketDisconnect:
        manager.disconnect(user.id)



async def _handle_dm(websocket: WebSocket, user: models.User, raw: dict, db: Session):
    try:
        incoming = schemas.ChatMessageIn.model_validate(raw)
    except ValidationError as e:
        await websocket.send_json({
            "kind": "error",
            "detail": e.errors(include_url=False, include_context=False),
        })
        return

    delivered_live = await manager.send_to_user(
        sender_id=user.id,
        recipient_id=incoming.to,
        content=incoming.message,
        db=db,
    )

    ack = schemas.ChatAck(to=incoming.to, delivered_live=delivered_live)
    await websocket.send_json(ack.model_dump(mode="json"))


async def _handle_group(websocket: WebSocket, user: models.User, raw: dict, db: Session):
    try:
        incoming = schemas.GroupChatMessageIn.model_validate(raw)
    except ValidationError as e:
        await websocket.send_json({
            "kind": "error",
            "detail": e.errors(include_url=False, include_context=False),
        })
        return

    try:
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



