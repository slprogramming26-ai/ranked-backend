from fastapi import APIRouter, Depends, status, HTTPException, Response
from fastapi.security.oauth2 import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from .. import database, schemas, models, utils, oauth2


router = APIRouter(tags=["Authentication"])

@router.post('/login')
def login(user_credentials: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(database.get_dp)):

    
    user = db.query(models.User).filter(models.User.email == user_credentials.username).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials"
        )
    
    # Prüfe das Passwort mit deiner verify-Funktion (Argon2)
    if not utils.verify_password(user_credentials.password, user.passwort):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="invalid credentials"
        )
    
    # Achtung: Korrigierter Funktionsname (access mit zwei 's')
    access_token = oauth2.create_access_token(data={"user_id": str(user.id)})
    
    return {
        "access_token": access_token, 
        "token_type": "bearer" # Leerzeichen entfernt
    }