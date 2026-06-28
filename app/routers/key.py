from fastapi import status, HTTPException, Depends, APIRouter
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typing import List
from .. import models, schemas, oauth2
from ..database import get_dp


router = APIRouter(
    prefix="/keys",
    tags=['Keys']
)

@router.put("/", response_model=schemas.PublicKeyOut)
def upload_my_public_key(
    payload: schemas.PublicKeyUpload,
    db: Session = Depends(get_dp),
    current_user: int = Depends(oauth2.get_current_user),
):
    # Gibt Schlüssel? Dann überschreiben, sonst neu anlegen.
    existing = db.query(models.UserKey).filter(
        models.UserKey.user_id == current_user.id
    ).first()

    if existing:
        existing.public_key = payload.public_key
    else:
        existing = models.UserKey(
            user_id=current_user.id,
            public_key=payload.public_key,
        )
        db.add(existing)

    db.commit()
    db.refresh(existing)
    return existing


@router.get("/{user_id}", response_model=schemas.PublicKeyOut)
def get_public_key(
    user_id: int,
    db: Session = Depends(get_dp),
    current_user: int = Depends(oauth2.get_current_user),
):
    key = db.query(models.UserKey).filter(
        models.UserKey.user_id == user_id
    ).first()

    if not key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dieser Nutzer hat noch keinen öffentlichen Schlüssel hinterlegt.",
        )

    return key

@router.post("/group/{group_chat_id}/rekey",
              response_model=schemas.GroupRekeyOut,
                status_code=status.HTTP_201_CREATED)

def rekey_group(
    group_chat_id: int,
    payload: schemas.GroupRekeyUpload,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    
    #checke gruppe existiert
    group = db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).first()
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"group chat {group_chat_id} not found",
        )
    
    #mitglied?
    members = db.query(models.GroupChatMembership.participant_id).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
    ).all()
    member_ids = {m.participant_id for m in members}
    if current_user.id not in member_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not a member of this group",
        )



    #integritäts-check: hochgeladene empfänger müssen GENAU die mitglieder sein
    #(keiner fehlt -> niemand wird ausgesperrt; keiner zu viel -> kein leak an nicht-mitglieder)
    recipient_ids = {c.recipient_id for c in payload.keys}
    if len(recipient_ids) != len(payload.keys):          # gleiche person doppelt?
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="duplicate recipient in keys",
        )
    if recipient_ids != member_ids:                       # zu wenige ODER zu viele?
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="key recipients do not match current members; refetch membership and retry",
        )


    #neue epoche
    current_version = db.query(func.max(models.GroupChatEpoch.key_version)).filter(
        models.GroupChatEpoch.group_chat_id == group_chat_id,
    ).scalar()
    new_version = (current_version or 0) + 1
    db.add(models.GroupChatEpoch(group_chat_id=group_chat_id, key_version=new_version))


    #für jeden ein key hochladen
    for copy in payload.keys:
        db.add(models.GroupChatKey(
            group_chat_id=group_chat_id,
            key_version=new_version,
            recipient_id=copy.recipient_id,
            encrypted_key=copy.encrypted_key,
        ))
    group.needs_rekey = False


    try:
        db.commit()
    except IntegrityError:
        #falls 2 gleichzeitig
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="concurrent rekey, please retry",
        )

    return {"key_version": new_version}


@router.get("/group/{group_chat_id}/key", response_model=schemas.GroupKeyOut)
def get_group_key(group_chat_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),):


    #checke gruppe existiert
    group = db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).first()
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"group chat {group_chat_id} not found",
        )
    
    #mitglied?
    member = db.query(models.GroupChatMembership.participant_id).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
          models.GroupChatMembership.participant_id == current_user.id
          ).first()
    
    if not member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not a member of this group",
        )
    
    # .scalar() liefert direkt die Zahl (oder None, wenn es noch keine Epoche gibt).
    current_version = db.query(func.max(models.GroupChatEpoch.key_version)).filter(
            models.GroupChatEpoch.group_chat_id == group_chat_id,
        ).scalar()

    if current_version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="group has no key epoch yet",
        )

    key = db.query(models.GroupChatKey
                   ).filter(models.GroupChatKey.group_chat_id == group_chat_id,
                             models.GroupChatKey.key_version == current_version,
                             models.GroupChatKey.recipient_id == current_user.id
                               ).first()
    
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="There was no new key found for you")
    
    return {
        "group_chat_id": group_chat_id,
        "key_version": key.key_version,
        "encrypted_key": key.encrypted_key
    }


@router.get("/group/{group_chat_id}/keys", response_model=List[schemas.GroupKeyOut])
def get_all_my_keys(group_chat_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),):

    #checke gruppe existiert
    group = db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).first()
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"group chat {group_chat_id} not found",
        )
    
    #mitglied?
    member = db.query(models.GroupChatMembership.participant_id).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
          models.GroupChatMembership.participant_id == current_user.id
          ).first()
    
    if not member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not a member of this group",
        )
    
    all_keys = db.query(models.GroupChatKey
                        ).filter(models.GroupChatKey.group_chat_id == group_chat_id,
                                 models.GroupChatKey.recipient_id == current_user.id
                                 ).order_by(models.GroupChatKey.key_version).all()

    # all_keys ist eine Liste von GroupChatKey-Objekten. Weil GroupKeyOut
    # from_attributes=True hat, serialisiert FastAPI jedes Objekt selbst.
    return all_keys

