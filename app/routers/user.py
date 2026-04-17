from fastapi import FastAPI, Response, status, HTTPException, Depends, APIRouter
from sqlalchemy.orm import Session
from typing import List, Optional


from sqlalchemy import func
from .. import models, schemas, oauth2,utils
from ..database import get_dp


router = APIRouter(
    prefix="/users",
    tags=['Users']
)

@router.post("/", status_code=status.HTTP_201_CREATED, response_model=schemas.UserOut)
def create_user(user: schemas.UserCreate, db: Session = Depends(get_dp)):


    existing_email = db.query(models.User).filter(models.User.email == user.email).first()
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User mit der E-Mail {user.email} existiert bereits."
        )

    existing_username = db.query(models.User).filter(models.User.username == user.username).first()
    if existing_username:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Der Username {user.username} ist leider schon vergeben."
        )

    hashed_passwort = utils.hash_password(user.passwort)
    user.passwort = hashed_passwort
    new_user = models.User(**user.dict())
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@router.get("/", response_model=schemas.GetUserOut)
def get_current_user(current_user: int = Depends(oauth2.get_current_user)):

    return current_user


@router.put("/")
def upgrade_user(user_details: schemas.UserDetails,current_user: int = Depends(oauth2.get_current_user),  db: Session = Depends(get_dp)):
    
    user_query = db.query(models.User).filter(models.User.id == current_user.id)
    user = user_query.first()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User doesnt exist")
    

    update_data = user_details.dict(exclude_unset=True)

    # 3. Update ausführen
    user_query.update(update_data, synchronize_session=False)
    
    db.commit()

    return {"status": "success", "updated_fields": list(update_data.keys())}

    
