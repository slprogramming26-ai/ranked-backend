from fastapi import status, HTTPException, Depends, APIRouter
from sqlalchemy.orm import Session

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
