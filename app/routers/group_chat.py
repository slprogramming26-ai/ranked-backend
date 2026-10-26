from fastapi import APIRouter, Depends, status, HTTPException, Response, Header, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.security.oauth2 import OAuth2PasswordRequestForm
from sqlalchemy import and_
from sqlalchemy.orm import Session
from typing import List, Optional
from .. import database, schemas, models, utils, oauth2
from ..database import get_dp
from datetime import timedelta, timezone, datetime
import secrets
from ..config import settings
from .user import _process_and_upload_image, delete_s3_object

def generate_unique_join_code(db: Session) -> int:
    for _ in range(5):
        code = secrets.randbelow(90_000_000) + 10_000_000
        exists = db.query(models.GroupChatJoinCodes).filter(
            models.GroupChatJoinCodes.code == code
        ).first()
        if not exists:
            return code
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="could not generate a unique join code",
    )


router = APIRouter(tags=["GroupChat"])


CODE_LIFETIME = timedelta(days=1)


def _get_group_and_membership(db: Session, group_chat_id: int, user_id: int):
    """Gruppe + Mitgliedschaft von user_id in EINER Abfrage (statt zwei Rundwegen).
    404, wenn die Gruppe nicht existiert. membership ist None, wenn user_id kein
    Mitglied ist -> was dann passiert (403/404), entscheidet die Route."""
    row = db.query(models.GroupChats, models.GroupChatMembership).outerjoin(
        models.GroupChatMembership,
        and_(
            models.GroupChatMembership.group_chat_id == models.GroupChats.group_chat_id,
            models.GroupChatMembership.participant_id == user_id,
        ),
    ).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"group chat {group_chat_id} not found")
    return row  # (group, membership)


def _get_membership(db: Session, group_chat_id: int, user_id: int):
    """Mitgliedschaft eines anderen Users (Ziel von kick/role/transfer) oder None."""
    return db.query(models.GroupChatMembership).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
        models.GroupChatMembership.participant_id == user_id,
    ).first()


def _effective_role(group, membership) -> Optional[str]:
    """'creator' | 'admin' | 'member', None für Nicht-Mitglieder.
    creator_id wird ZUERST geprüft -> die role-Zeile des Gründers wird ignoriert."""
    if membership is None:
        return None
    if membership.participant_id == group.creator_id:
        return "creator"
    return membership.role

#crud for groups

@router.post("/group_chat/create", status_code=status.HTTP_201_CREATED)
def create_group_chat(
    group_create: Optional[schemas.GroupChatCreate] = None,  # ohne Body -> None (wie bisher)
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    group_name = group_create.group_name if group_create else None
    group = models.GroupChats(creator_id=current_user.id, group_name=group_name)
    db.add(group)
    # flush statt commit: schickt das INSERT ab (-> group_chat_id ist da), aber
    # die Transaktion bleibt offen. Gruppe + Mitgliedschaft landen zusammen oder gar nicht.
    db.flush()
    group_chat_id = group.group_chat_id

    db.add(models.GroupChatMembership(
        group_chat_id=group_chat_id,
        participant_id=current_user.id,
    ))
    db.commit()

    return {"group_chat_id": group_chat_id}


@router.post("/group_chat/group_chat_join_code/{group_chat_id}", status_code=status.HTTP_201_CREATED, response_model=schemas.GroupChatJoinCodeOut)
def create_group_chat_join_code(
    group_chat_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    
    #existiert die gruppe
    group = db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == group_chat_id,
    ).first()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"group chat {group_chat_id} not found")

    #ist die person mitglied
    member = db.query(models.GroupChatMembership).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
        models.GroupChatMembership.participant_id == current_user.id,
    ).first()
    if not member:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You cannot create a code for a group you are not a part of")


    #hier muss ein code erzeugt werden er ist einzigartig (primary_key = True). er sollte 6-8 stellen haben bestenfalls.
    code = generate_unique_join_code(db)
    code_entry = models.GroupChatJoinCodes(code = code, group_chat_id = group_chat_id)
    db.add(code_entry)
    db.commit()
    db.refresh(code_entry)

    return code_entry
    



@router.post("/group_chat/join/{group_chat_join_code}", status_code=status.HTTP_201_CREATED, response_model=schemas.GroupChatJoinOut)
def join_group_chat(
    group_chat_join_code: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    #expried?
    cutoff = datetime.now(timezone.utc) - CODE_LIFETIME


    #gibt es einen solchen aktuellen code?
    code = db.query(models.GroupChatJoinCodes).filter(
        models.GroupChatJoinCodes.code == group_chat_join_code,
        models.GroupChatJoinCodes.created_at >= cutoff,
    ).first()
    if not code:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"code {group_chat_join_code} does not exist")

    #ist die person mitglied
    already = db.query(models.GroupChatMembership).filter(
        models.GroupChatMembership.group_chat_id == code.group_chat_id,
        models.GroupChatMembership.participant_id == current_user.id,
    ).first()
    if already:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already a member")

    #rekey aktivieren
    db.query(models.GroupChats).filter(
        models.GroupChats.group_chat_id == code.group_chat_id,
    ).update({"needs_rekey": True})

    #joinen
    db.add(models.GroupChatMembership(
        group_chat_id=code.group_chat_id,
        participant_id=current_user.id,
    ))
    db.commit()
    return {"message": "joined", "group_chat_id": code.group_chat_id} 



@router.delete("/group_chat/leave/{group_chat_id}")
def leave_group_chat(
    group_chat_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    group, membership = _get_group_and_membership(db, group_chat_id, current_user.id)

    if group.creator_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="creator cannot leave; transfer the group to another member first",
        )

    if not membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not a member of this group")

    group.needs_rekey = True
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

    group, membership = _get_group_and_membership(db, group_chat_id, current_user.id)
    my_role = _effective_role(group, membership)
    if my_role not in ("creator", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not allowed to perform this action")

    if user_id == group.creator_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the creator cannot be kicked")

    target = _get_membership(db, group_chat_id, user_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"user with id: {user_id} does not exist in this group")

    # Admins dürfen nur normale Mitglieder rauswerfen (keine Admins, auch nicht sich selbst).
    if my_role == "admin" and target.role != "member":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admins can only kick members")

    group.needs_rekey = True
    db.delete(target)
    db.commit()
    return {"message": f"user with id {user_id} successfully kicked"}


@router.patch("/group_chat/{group_chat_id}/members/{user_id}/role")
def change_member_role(
    group_chat_id: int,
    user_id: int,
    role_update: schemas.GroupMemberRoleUpdate,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    group, membership = _get_group_and_membership(db, group_chat_id, current_user.id)
    my_role = _effective_role(group, membership)
    if my_role not in ("creator", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not allowed to perform this action")

    # Gründer wechselt nur über /transfer, nicht über die Rolle.
    if user_id == group.creator_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the creator's role cannot be changed")

    target = _get_membership(db, group_chat_id, user_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"user with id: {user_id} does not exist in this group")

    # Befördern dürfen Gründer + Admins, degradieren nur der Gründer.
    if role_update.role == "member" and my_role != "creator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the creator can demote admins")

    # Ist die Rolle schon gesetzt, ändert sich nichts -> trotzdem 200.
    target.role = role_update.role
    db.commit()
    return {"user_id": user_id, "role": role_update.role}


@router.post("/group_chat/{group_chat_id}/transfer/{user_id}")
def transfer_group(
    group_chat_id: int,
    user_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    group, membership = _get_group_and_membership(db, group_chat_id, current_user.id)
    if group.creator_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the creator can transfer the group")

    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="you are already the creator")

    target = _get_membership(db, group_chat_id, user_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"user with id: {user_id} does not exist in this group")

    # Beide Änderungen landen in EINEM commit -> nie zwei Gründer oder keiner.
    group.creator_id = user_id
    membership.role = "admin"  # alter Gründer bleibt Admin, kann danach per leave gehen
    target.role = "admin"      # falls er später selbst weiter übergibt, ist er danach Admin
    db.commit()
    return {"group_chat_id": group_chat_id, "creator_id": user_id}

    






@router.get("/group_chat/{group_chat_id}/members", response_model=List[schemas.GroupMemberOut])
def get_group_members(
    group_chat_id: int,
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    group, membership = _get_group_and_membership(db, group_chat_id, current_user.id)

    # Nur Mitglieder dürfen die Mitgliederliste sehen.
    if not membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a member of this group")

    # Mitgliedschaften mit den User-Daten joinen.
    rows = db.query(
        models.User.id, models.User.username, models.User.profile_picture_url,
        models.GroupChatMembership.role,
    ).join(
        models.GroupChatMembership, models.GroupChatMembership.participant_id == models.User.id,
    ).filter(
        models.GroupChatMembership.group_chat_id == group_chat_id,
    ).all()

    # User.id heißt im Schema user_id -> einmal umbenennen.
    # Gründer steht in creator_id, nicht in role -> hier einsetzen.
    return [
        {
            "user_id": r.id,
            "username": r.username,
            "profile_picture_url": r.profile_picture_url,
            "role": "creator" if r.id == group.creator_id else r.role,
        }
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


    # URL VOR dem Löschen merken: nach commit ist group expired und die Zeile weg.
    picture_url = group.profile_picture
    db.delete(group)
    db.commit()

    delete_s3_object(picture_url, db)
    db.commit()  # speichert ggf. den FailedImageDeletions-Eintrag
    return {"message": "deleted"}

@router.patch("/group_chat/{group_chat_id}", status_code=status.HTTP_200_OK)
def update_group_chat(
    group_chat_id: int,
    group_update: schemas.GroupChatUpdate,  
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    # 1. Gruppe + eigene Mitgliedschaft holen (404, falls die Gruppe nicht existiert)
    group, membership = _get_group_and_membership(db, group_chat_id, current_user.id)

    # 2. Berechtigung prüfen (Gründer oder Admin)
    if _effective_role(group, membership) not in ("creator", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the creator or an admin can update group details"
        )

    # 3. Nur gesendete Daten (exclude_unset=True) herausfiltern
    update_data = group_update.model_dump(exclude_unset=True)

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No data provided to update"
        )

    # 4. Gesendete Felder auf die geladene Gruppe schreiben und speichern
    for key, value in update_data.items():
        setattr(group, key, value)
    db.commit()
    db.refresh(group)

    return {"message": "Group updated successfully", "group": schemas.GroupChatInformationOut.model_validate(group)}


@router.post("/group_chat/{group_chat_id}/picture", response_model=schemas.GroupChatInformationOut)
async def upload_group_picture(
    group_chat_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_dp),
    current_user=Depends(oauth2.get_current_user),
):
    # 1. Gruppe + Berechtigung ZUERST prüfen -> kein Upload, der danach verwaist.
    group, membership = _get_group_and_membership(db, group_chat_id, current_user.id)

    if _effective_role(group, membership) not in ("creator", "admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="only the creator or an admin can change the group picture")

    # 2. Dateigröße prüfen (max 15MB)
    MAX_SIZE = 15 * 1024 * 1024
    contents = await file.read(MAX_SIZE + 1)
    if len(contents) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="Datei zu groß! Max 15MB.")

    # 3. Content-Type prüfen
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Nur Bilder erlaubt!")

    # 4. Den schweren Teil im Threadpool ausführen -> Event-Loop bleibt frei.
    try:
        url = await run_in_threadpool(_process_and_upload_image, contents, "group_picture")
    except HTTPException:
        raise  # echte Validierungsfehler (z.B. zu viele Pixel) durchlassen
    except Exception as e:
        print(f"Fehler beim Gruppenbild-Upload: {e}")
        raise HTTPException(status_code=500, detail="Bildverarbeitung fehlgeschlagen.")

    # 5. Neue URL speichern, danach das alte Bild aus S3 entfernen.
    old_url = group.profile_picture
    group.profile_picture = url
    db.commit()
    db.refresh(group)

    delete_s3_object(old_url, db)
    db.commit()  # speichert ggf. den FailedImageDeletions-Eintrag

    return group



@router.delete("/group_chat/cleanup/expired_join_codes")
def cleanup_expired_join_codes(
    db: Session = Depends(get_dp),
    x_group_join_code_cleanup_secret: Optional[str] = Header(default=None),
):
    """Löscht alle Join-Codes, die älter als CODE_LIFETIME sind. Wird NICHT von
    Usern aufgerufen, sondern von einem externen Scheduler (Supabase pg_cron /
    cron-job.org) einmal pro Tag. Geschützt durch Header-Secret, kein Login-Token
    (wie Story-Cleanup / Ranking-Job)."""
    if x_group_join_code_cleanup_secret != settings.group_join_code_cleanup_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid cleanup secret")

    cutoff = datetime.now(timezone.utc) - CODE_LIFETIME

    deleted_count = db.query(models.GroupChatJoinCodes).filter(
        models.GroupChatJoinCodes.created_at < cutoff
    ).delete(synchronize_session=False)
    db.commit()

    return {"message": f"{deleted_count} join code(s) deleted"}



