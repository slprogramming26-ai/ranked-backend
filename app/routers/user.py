from fastapi import FastAPI, Response, status, HTTPException, Depends, APIRouter, UploadFile, File
from sqlalchemy.orm import Session
from typing import List, Optional
from PIL import Image
import io
import boto3
from ..config import settings

from sqlalchemy import func
from .. import models, schemas, oauth2,utils
from ..database import get_dp


router = APIRouter(
    prefix="/users",
    tags=['Users']
)


S3_ENDPOINT = f'{settings.s3_endpoint}'
S3_ACCESS_KEY = f'{settings.s3_access_key}'
S3_SECRET_KEY = f'{settings.s3_secret_key}'
BUCKET_NAME = 'user_images'

s3_client = boto3.client(
    's3',
    endpoint_url=f"{S3_ENDPOINT}", # Wichtig: s3 Pfad anhängen
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY
)




@router.post("/upload")
async def upload_user_image(
    file: UploadFile = File(...), 
    current_user: int = Depends(oauth2.get_current_user)
):
    MAX_SIZE = 15 * 1024 * 1024
    print(f"Content-Type von Flutter: '{file.content_type}'")

    # 1. Einmal lesen
    contents = await file.read(MAX_SIZE + 1)
    if len(contents) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="Datei zu groß! Max 5MB.")

    # 2. Content-Type prüfen
    content_type = (file.content_type or "").strip()
    if not content_type or not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Nur Bilder erlaubt!")

    try:
        # 3. Pillow Verarbeitung
        img = Image.open(io.BytesIO(contents))
        img.verify()
        img = Image.open(io.BytesIO(contents))  # Nach verify() neu laden

        if img.width * img.height > 20_000_000:
            raise HTTPException(status_code=400, detail="Bild hat zu viele Pixel!")

        img.thumbnail((1024, 1024))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        buffer.seek(0)

        # 4. Upload zu Supabase
        file_name = f"profile_picture/{file.filename}"
        s3_client.upload_fileobj(
            buffer,
            BUCKET_NAME,
            file_name,
            ExtraArgs={'ACL': 'public-read', 'ContentType': 'image/jpeg'}
        )

        url = f"https://yrnrhjvauknhlotoqpea.supabase.co/storage/v1/object/public/{BUCKET_NAME}/{file_name}"
        return {"image_url": url}

    except Exception as e:
        print(f"Fehler beim Upload: {e}")
        raise HTTPException(status_code=500, detail="Bildverarbeitung fehlgeschlagen.")
    



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
def get_current_user(current_user: int = Depends(oauth2.get_current_user),  db: Session = Depends(get_dp)):

    follower_count = db.query(models.Follows).filter(models.Follows.followee_id == current_user.id).count()

    return {"email": current_user.email,
            "username": current_user.username,
            "vibe_factor_1": current_user.vibe_factor_1,
            "vibe_factor_2": current_user.vibe_factor_2,
            "profile_picture_url": current_user.profile_picture_url,
            "biography": current_user.biography,
            "ranking_enabled": current_user.ranking_enabled,
            "follower_count": follower_count
            }


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

    
