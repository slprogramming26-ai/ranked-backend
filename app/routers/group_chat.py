from fastapi import APIRouter, Depends, status, HTTPException, Response
from fastapi.security.oauth2 import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from typing import List
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

    db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).update({"needs_rekey": True})
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
    group = db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).first()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"group chat {group_chat_id} not found")

    if group.creator_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="creator cannot leave; delete the group instead",
        )

    membership = db.query(models.GroupChatMembership).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
        models.GroupChatMembership.participant_id == current_user.id,
    ).first()
    if not membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not a member of this group")

    db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).update({"needs_rekey": True})

    db.delete(membership)
    db.commit()
    return {"message": "left"}

@router.get("/group_chat/my", response_model=List[schemas.GroupChatInformationOut])
def get_all_my_groups(
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    all_my_groups = db.query(
        models.GroupChats.group_chat_id,
        models.GroupChats.group_name,
        models.GroupChats.profile_picture,
    ).join(
        models.GroupChatMembership,
        models.GroupChatMembership.group_chat_id == models.GroupChats.group_chat_id,
    ).filter(
        models.GroupChatMembership.participant_id == current_user.id,
    ).all()

    return all_my_groups

@router.delete("/group_chat/kick/{group_chat_id}/{user_id}")
def kick_from_group(group_chat_id: int,
                    user_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),):

    group = db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).first()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"group chat {group_chat_id} not found")
    
    if group.creator_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
                             detail="You are not allowed to perform this action")

    # Creator kann sich nicht selbst kicken -> sonst bliebe creator_id ohne Mitgliedschaft.
    if user_id == group.creator_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the creator cannot be kicked; delete the group instead")

    member = db.query(models.GroupChatMembership
                      ).filter(models.GroupChatMembership.group_chat_id == group_chat_id,
                               models.GroupChatMembership.participant_id == user_id).first()
    
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
              detail=f"user with id: {user_id} does not exist in this group")

    
    db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).update({"needs_rekey": True})
    db.delete(member)
    db.commit()
    return {"message": f"user with id {user_id} successfully kicked"}
    






@router.get("/group_chat/{group_chat_id}/members", response_model=List[schemas.GroupMemberOut])
def get_group_members(
    group_chat_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    group = db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).first()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"group chat {group_chat_id} not found")

    # Nur Mitglieder dürfen die Mitgliederliste sehen.
    is_member = db.query(models.GroupChatMembership).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
        models.GroupChatMembership.participant_id == current_user.id,
    ).first()
    if not is_member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a member of this group")

    # Mitgliedschaften mit den User-Daten joinen.
    rows = db.query(
        models.User.id, models.User.username, models.User.profile_picture_url,
    ).join(
        models.GroupChatMembership, models.GroupChatMembership.participant_id == models.User.id,
    ).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
    ).all()

    # User.id heißt im Schema user_id -> einmal umbenennen.
    return [
        {"user_id": r.id, "username": r.username, "profile_picture_url": r.profile_picture_url}
        for r in rows
    ]



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

