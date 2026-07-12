from fastapi import FastAPI, Response, status, HTTPException, Depends, APIRouter, Query, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from typing import List, Optional
from PIL import Image
import io
import boto3
from ..config import settings
import uuid
from sqlalchemy import func
from .. import models, schemas, oauth2,utils
from ..database import get_dp
from ..xp_config import get_league, get_effective_streak
from .post import delete_s3_object as delete_post_image



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

def delete_s3_object(image_url: str | None):
    """Löscht eine Datei aus S3 anhand ihrer öffentlichen URL. Schluckt Fehler bewusst."""
    if not image_url:
        return
    marker = f"/public/{BUCKET_NAME}/"
    idx = image_url.find(marker)
    if idx == -1:
        return
    s3_key = image_url[idx + len(marker):]
    try:
        s3_client.delete_object(Bucket=BUCKET_NAME, Key=s3_key)
    except Exception as e:
        print(f"Warnung: S3-Bild konnte nicht gelöscht werden: {e}")


def _process_and_upload_image(contents: bytes) -> str:
    """Der BLOCKIERENDE Teil: Pillow-Verarbeitung (CPU) + S3-Upload (sync I/O).
    Läuft im Threadpool (siehe run_in_threadpool unten), damit der Event-Loop
    NICHT einfriert, während ein Bild verarbeitet/hochgeladen wird.
    Gibt die öffentliche URL des Bildes zurück."""
    # Pillow Verarbeitung
    img = Image.open(io.BytesIO(contents))
    img.verify()  # Validieren

    # Nach verify() muss das Objekt neu geladen werden
    img = Image.open(io.BytesIO(contents))

    # Decompression Bomb Schutz
    if img.width * img.height > 20_000_000:
        raise HTTPException(status_code=400, detail="Bild hat zu viele Pixel!")

    # Resize & Kompression
    img.thumbnail((1024, 1024))
    if img.mode in ("RGBA", "P"):  # Transparent-Support zu RGB konvertieren
        img = img.convert("RGB")

    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=85)
    buffer.seek(0)

    # Upload zu Supabase
    file_name = f"profile_picture/{uuid.uuid4().hex}.jpg"
    s3_client.upload_fileobj(
        buffer,
        BUCKET_NAME,
        file_name,
        ExtraArgs={'ACL': 'public-read', 'ContentType': 'image/jpeg'}
    )

    return f"{settings.supabase_url}/storage/v1/object/public/{BUCKET_NAME}/{file_name}"



@router.post("/upload")
async def upload_user_image(
    file: UploadFile = File(...), 
    current_user: int = Depends(oauth2.get_current_user)
):
    MAX_SIZE = 15 * 1024 * 1024
    print(f"Content-Type von Flutter: '{file.content_type}'")
    contents = await file.read(MAX_SIZE + 1)
    if len(contents) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="Datei zu groß! Max 5MB.")

    # 2. Content-Type prüfen
    # Hinweis: Wenn Flutter den Content-Type nicht sendet,
    # wird dieser Check wieder fehlschlagen (Error 400).
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Nur Bilder erlaubt!")

    # 3. Den schweren Teil im Threadpool ausführen -> Event-Loop bleibt frei.
    try:
        url = await run_in_threadpool(_process_and_upload_image, contents)
    except HTTPException:
        raise  # echte Validierungsfehler (z.B. zu viele Pixel) durchlassen
    except Exception as e:
        print(f"Fehler beim Upload: {e}")
        raise HTTPException(status_code=500, detail="Bildverarbeitung fehlgeschlagen.")

    return {"image_url": url}
    



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

    return {"id": current_user.id,
        "email": current_user.email,
            "username": current_user.username,
            "vibe_factor_1": current_user.vibe_factor_1,
            "vibe_factor_2": current_user.vibe_factor_2,
            "profile_picture_url": current_user.profile_picture_url,
            "biography": current_user.biography,
            "location": current_user.location,
            "ranking_enabled": current_user.ranking_enabled,
            "follower_count": follower_count,
            "xp": current_user.xp,
            "streak_count": get_effective_streak(current_user.streak_count, current_user.last_swipe_date),
            "league": get_league(current_user.xp),
            }

@router.get("/search", response_model=List[schemas.GetUserOut])
def search_for_user(
    search: str = Query(..., min_length=2, max_length=30),
      current_user: int = Depends(oauth2.get_current_user),
      db: Session = Depends(get_dp)
  ):
      pattern = f"%{search}%"
      prefix = f"{search}%"
 
      found_users = (
         db.query(models.User)
          .filter(models.User.username.ilike(pattern))
          .filter(models.User.id != current_user.id)
          .order_by(
              models.User.username.ilike(prefix).desc(),
              func.length(models.User.username).asc(),
              models.User.username.asc(),
          )
          .limit(20)
          .all()
      )
 
      return [{
          "id": user.id,
          "username": user.username,
          "vibe_factor_1": user.vibe_factor_1,
          "vibe_factor_2": user.vibe_factor_2,
          "profile_picture_url": user.profile_picture_url,
          "biography": user.biography,
          "ranking_enabled": user.ranking_enabled,
      } for user in found_users]



@router.get("/{id}", response_model=schemas.GetUserOut)
def get_user(id: int, current_user: int = Depends(oauth2.get_current_user),  db: Session = Depends(get_dp)):

    target_user = db.query(models.User).filter(models.User.id == id).first()

    if not target_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User with id: {id} was not found")
    
    follower_count = db.query(models.Follows).filter(models.Follows.followee_id == id).count()
    follow_query = db.query(models.Follows).filter(models.Follows.followee_id == id, models.Follows.follower_id == current_user.id).first()
    following = False

    if follow_query:
        following = True



    return {'id': target_user.id,
        "username": target_user.username,
            "vibe_factor_1": target_user.vibe_factor_1,
            "vibe_factor_2": target_user.vibe_factor_2,
            "profile_picture_url": target_user.profile_picture_url,
            "biography": target_user.biography,
            "ranking_enabled": target_user.ranking_enabled,
            "follower_count": follower_count,
            "is_followed": following,
            "xp": target_user.xp,
            "streak_count": get_effective_streak(target_user.streak_count, target_user.last_swipe_date),
            "league": get_league(target_user.xp),
            }






@router.put("/")
def upgrade_user(user_details: schemas.UserDetails,current_user: int = Depends(oauth2.get_current_user),  db: Session = Depends(get_dp)):
    
    user_query = db.query(models.User).filter(models.User.id == current_user.id)
    user = user_query.first()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User doesnt exist")
    

    update_data = user_details.dict(exclude_unset=True)

    if update_data.get("location_id") is not None:
        location_exists = db.query(models.Location).filter(models.Location.id == update_data["location_id"]).first()
        if not location_exists:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Location does not exist")



    # 3. Update ausführen
    user_query.update(update_data, synchronize_session=False)
    
    db.commit()

    return {"status": "success", "updated_fields": list(update_data.keys())}


@router.delete("/", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(current_user = Depends(oauth2.get_current_user),
                   db: Session = Depends(get_dp)):
    # 1. Bilder ALLER Posts des Users einsammeln und aus S3 löschen
    posts = db.query(models.Post).filter(models.Post.owner_id == current_user.id).all()
    for post in posts:
        delete_s3_object(post.image_url)

    # 2. Profilbild löschen
    delete_s3_object(current_user.profile_picture_url)

    # 3. User aus DB löschen -> CASCADE räumt posts/votes/comments automatisch auf
    db.query(models.User).filter(models.User.id == current_user.id) \
        .delete(synchronize_session=False)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)



@router.post("/block/{id}", status_code=status.HTTP_201_CREATED)
def block_user(id: int, current_user: int = Depends(oauth2.get_current_user), db: Session = Depends(get_dp)):

    if id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot block yourself")
    
    user = db.query(models.User).filter(models.User.id == id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User doesnt exist") 
    
    blocked_user = db.query(models.Block).filter(
        models.Block.blocker_id == current_user.id, 
        models.Block.blocked_id == id
    ).first()

    if blocked_user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You already blocked that user")
    

    new_blocked_user = models.Block(blocker_id=current_user.id, blocked_id=id)

    db.add(new_blocked_user)
    db.commit()
    db.refresh(new_blocked_user)

    return {"message": f"You blocked user {id} successfully"}


@router.delete("/block/{id}")
def delete_user_block(id: int, current_user: int = Depends(oauth2.get_current_user), db: Session = Depends(get_dp)):

    user = db.query(models.User).filter(models.User.id == id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User doesnt exist") 
    
    
    blocked_user = db.query(models.Block).filter(
        models.Block.blocker_id == current_user.id, 
        models.Block.blocked_id == id
    ).first()

    if not blocked_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No blocked user with id: {id} found")
    
    db.delete(blocked_user)
    db.commit()

    return {"message": f"User {id} no longer blocked"}
    





