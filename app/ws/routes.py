from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query, status
from sqlalchemy.orm import Session
from jose import JWTError, jwt
from .. import models, oauth2
from ..database import get_dp
from .manager import manager


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
        # 1008 = Policy Violation (passt für Auth-Fehler)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket)
    await manager.send_to_everyone(f"*** {user.username} joined the chat ***")
    try:
        while True:
            data = await websocket.receive_text()
            await manager.send_to_everyone(f"{user.username}: {data}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        await manager.send_to_everyone(f"*** {user.username} left the chat ***")
