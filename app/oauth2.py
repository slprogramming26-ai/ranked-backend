import secrets
import hashlib
from jose import JWTError, jwt
from . import schemas, database, models
from datetime import datetime, timedelta
from fastapi import Depends, status, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from .config import settings

oauth_scheme = OAuth2PasswordBearer(tokenUrl="login")

SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes
REFRESH_TOKEN_EXPIRE_DAYS = settings.refresh_token_expire_days

def create_access_token(data: dict):
    to_encode = data.copy()

    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

    return encoded_jwt



def verify_access_token(token: str, credentials_exception):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        # Wir holen die ID aus dem Payload
        user_id: str = payload.get("user_id")

        if user_id is None:
            raise credentials_exception
        
        # Sicherstellen, dass die Daten dem Schema entsprechen
        token_data = schemas.TokenData(id=user_id)
    except JWTError:
        raise credentials_exception
    
    return token_data

def get_current_user(token: str = Depends(oauth_scheme), db: Session = Depends(database.get_dp)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, 
        detail="could not validate credentials", 
        headers={"WWW-Authenticate": "Bearer"}
    )

    token_data = verify_access_token(token, credentials_exception)

    # Hier nutzen wir token_data.id, um den User aus der DB zu laden
    user = db.query(models.User).filter(models.User.id == token_data.id).first()

    if user is None:
        raise credentials_exception

    return user


def hash_token(token: str) -> str:
    # SHA-256: macht aus dem Klartext-Token einen festen Hash. Nicht umkehrbar.
    return hashlib.sha256(token.encode()).hexdigest()


def create_refresh_token(user_id: int, db: Session) -> str:
    # 1. Zufalls-String erzeugen (das ist der eigentliche Refresh Token)
    raw_token = secrets.token_urlsafe(64)

    # 2. Nur den Hash + Ablaufdatum in die DB speichern
    expires_at = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    db_token = models.RefreshToken(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
    )
    db.add(db_token)
    db.commit()

    # 3. Den KLARTEXT zurückgeben – nur der User bekommt ihn
    return raw_token


def verify_refresh_token(token: str, db: Session, credentials_exception):
    # Wir hashen den vom User geschickten Token und suchen den Hash in der DB
    db_token = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == hash_token(token)
    ).first()

    # Nicht gefunden (z.B. nach Logout) oder abgelaufen -> ungültig
    if db_token is None:
        raise credentials_exception

    if db_token.expires_at < datetime.now(db_token.expires_at.tzinfo):
        raise credentials_exception

    return db_token