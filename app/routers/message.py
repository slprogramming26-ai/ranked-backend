from fastapi import FastAPI, Response, status, HTTPException, Depends, APIRouter, Query, UploadFile, File
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from PIL import Image
import io
import boto3
from ..config import settings
import uuid
from sqlalchemy import func, or_
from .. import models, schemas, oauth2,utils
from ..database import get_dp
from .post import delete_s3_object as delete_post_image



router = APIRouter(
    prefix="/messages",
    tags=['Messages']
)

@router.get("/", response_model=List[schemas.MessageOut])
def get_messages(
    since: Optional[datetime] = None,
    db: Session = Depends(get_dp),
    current_user: int = Depends(oauth2.get_current_user),
):
    query = db.query(models.Message).filter(                                                                                                                                                                                         
        or_(                                                                                                                                                                                                                         
            models.Message.recipient_id == current_user.id,                                                                                                                                                                          
            models.Message.sender_id == current_user.id,                                                                                                                                                                             
        )                                                                                                                                                                                                                            
    )      

    if since is not None:
        query = query.filter(models.Message.created_at > since)

    # Immer sortiert ausliefern, damit der Client die Reihenfolge nicht selbst herstellen muss.
    return query.order_by(models.Message.created_at).all()


@router.get("/group/{id}", response_model=List[schemas.GroupMessageOut])
def get_group_messages(
    id: int,
    since: Optional[datetime] = None,
    db: Session = Depends(get_dp), 
    current_user: int = Depends(oauth2.get_current_user),
              ):
    
    
    group_membership = db. query(models.GroupChatMembership).filter(models.GroupChatMembership.group_chat_id == id, models.GroupChatMembership.participant_id == current_user.id).first()
    
    if not group_membership:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="You cannot get messages of a group you are not part of")
    
    # joined_at ist eine UNTERGRENZE, keine Erlaubnis-Prüfung:
    # Du siehst grundsätzlich nur Nachrichten ab deinem Beitritt — immer, auch beim ersten Sync.
    query = db.query(models.GroupMessage).filter(
        models.GroupMessage.group_chat_id == id,
        models.GroupMessage.created_at >= group_membership.joined_at,
    )

    # since grenzt zusätzlich ein: nur Neueres als das, was der Client schon hat.
    if since is not None:
        query = query.filter(models.GroupMessage.created_at > since)

    return query.order_by(models.GroupMessage.created_at).all()
