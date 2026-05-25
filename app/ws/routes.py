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


#crud for groups

@router.post("/group_chat/create", status_code=status.HTTP_201_CREATED)
def create_group_chat(
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    group = models.GroupChats(creator_id=current_user.id)
    db.add(group)
    db.commit()
    db.refresh(group)


    db.add(models.GroupChatMembership(
        group_chat_id=group.group_chat_id,
        participant_id=current_user.id,
    ))
    db.commit()

    return {"group_chat_id": group.group_chat_id}


@router.post("/group_chat/join/{group_chat_id}", status_code=status.HTTP_201_CREATED)
def join_group_chat(
    group_chat_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    
    group = db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).first()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"group chat {group_chat_id} not found")

    already = db.query(models.GroupChatMembership).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
        models.GroupChatMembership.participant_id == current_user.id,
    ).first()
    if already:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already a member")

    db.add(models.GroupChatMembership(
        group_chat_id=group_chat_id,
        participant_id=current_user.id,
    ))
    db.commit()
    return {"message": "joined"}


@router.delete("/group_chat/leave/{group_chat_id}")
def leave_group_chat(
    group_chat_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    
    membership = db.query(models.GroupChatMembership).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
        models.GroupChatMembership.participant_id == current_user.id,
    ).first()
    if not membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not a member of this group")

    db.delete(membership)
    db.commit()
    return {"message": "left"}


@router.delete("/group_chat/{group_chat_id}")
def delete_group_chat(
    group_chat_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    group = db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).first()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"group chat {group_chat_id} not found")

    if group.creator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="only the creator can delete this group")

    db.delete(group)
    db.commit()
    return {"message": "deleted"}
