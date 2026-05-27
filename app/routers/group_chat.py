from fastapi import APIRouter, Depends, status, HTTPException, Response
from fastapi.security.oauth2 import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from .. import database, schemas, models, utils, oauth2
from ..database import get_dp


router = APIRouter(tags=["GroupChat"])
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

@router.patch("/group_chat/{group_chat_id}", status_code=status.HTTP_200_OK)
def update_group_chat(
    group_chat_id: int,
    group_update: schemas.GroupChatUpdate,  
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    # 1. Suchen der Gruppe in der Datenbank
    group_query = db.query(models.GroupChats).filter(models.GroupChats.group_chat_id == group_chat_id)
    group = group_query.first()

    # 2. Fehler werfen, falls die Gruppe nicht existiert
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"Group chat with id {group_chat_id} not found"
        )

    # 3. Berechtigung prüfen (nur der Ersteller darf editieren)
    if group.creator_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Only the creator can update group details"
        )

    # 4. Nur gesendete Daten (exclude_unset=True) herausfiltern
    update_data = group_update.model_dump(exclude_unset=True)
    
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="No data provided to update"
        )

    # 5. Spalten in der Datenbank aktualisieren und speichern
    group_query.update(update_data, synchronize_session=False)
    db.commit()
    db.refresh(group)

    return {"message": "Group updated successfully", "group": schemas.GroupChatOut.model_validate(group)}

