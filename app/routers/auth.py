from fastapi import APIRouter, Depends, status, HTTPException, Response, Request
from fastapi.security.oauth2 import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from .. import database, schemas, models, utils, oauth2
from ..limiter import limiter


router = APIRouter(tags=["Authentication"])

@router.post('/login', response_model=schemas.Token)
@limiter.limit("5/minute")
def login(request: Request, user_credentials: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(database.get_dp)):

    
    user = db.query(models.User).filter(models.User.email == user_credentials.username).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials"
        )
    
    # Prüfe das Passwort
    if not utils.verify_password(user_credentials.password, user.passwort):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="invalid credentials"
        )
    
    access_token = oauth2.create_access_token(data={"user_id": str(user.id)})

    # Langlebiger Refresh Token 
    refresh_token = oauth2.create_refresh_token(user_id=user.id, db=db)

    return {"access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer"
        }


@router.post('/refresh', response_model=schemas.Token)
def refresh(token_request: schemas.RefreshRequest, db: Session = Depends(database.get_dp)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or expired refresh token",
        headers={"WWW-Authenticate": "Bearer"}
    )

    # 1. Refresh Token prüfen (existiert in DB? nicht abgelaufen?)
    db_token = oauth2.verify_refresh_token(token_request.refresh_token, db, credentials_exception)

    # user_id merken, BEVOR wir die Zeile löschen
    user_id = db_token.user_id

    # 2. Rotation: alten Refresh Token aus der DB entfernen
    db.delete(db_token)
    db.commit()

    # 3. Neues Token-Paar ausstellen
    new_access_token = oauth2.create_access_token(data={"user_id": str(user_id)})
    new_refresh_token = oauth2.create_refresh_token(user_id=user_id, db=db)

    return {"access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer"
        }


@router.post('/logout', status_code=status.HTTP_204_NO_CONTENT)
def logout(token_request: schemas.RefreshRequest, db: Session = Depends(database.get_dp)):
    # Refresh Token aus der DB löschen -> kann keinen neuen Access Token mehr holen
    db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == oauth2.hash_token(token_request.refresh_token)
    ).delete()
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)