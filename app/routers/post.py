from fastapi import FastAPI, Response, status, HTTPException, Depends, APIRouter, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from typing import List, Optional
from PIL import Image
import io
import boto3
from ..config import settings
import uuid
from sqlalchemy import func
from .. import models, schemas, oauth2
from ..database import get_dp

router = APIRouter(
    prefix="/posts",
    tags=['Posts']
)




S3_ENDPOINT = f'{settings.s3_endpoint}'
S3_ACCESS_KEY = f'{settings.s3_access_key}'
S3_SECRET_KEY = f'{settings.s3_secret_key}'
BUCKET_NAME = 'post_images'

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
    file_name = f"posts/{uuid.uuid4().hex}.jpg"
    s3_client.upload_fileobj(
        buffer,
        BUCKET_NAME,
        file_name,
        ExtraArgs={'ACL': 'public-read', 'ContentType': 'image/jpeg'}
    )

    return f"https://yrnrhjvauknhlotoqpea.supabase.co/storage/v1/object/public/{BUCKET_NAME}/{file_name}"


@router.post("/upload")
async def upload_post_image(
    file: UploadFile = File(...),
    current_user: int = Depends(oauth2.get_current_user)
):
    # 1. Dateigröße prüfen (max 5MB)
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
    







@router.get("/", response_model=List[schemas.PostOut])
def get_posts(db: Session = Depends(get_dp), 
              current_user: int = Depends(oauth2.get_current_user), 
              limit: int = 10, 
              skip: int = 0, 
              search: Optional[str] = ""):

    posts = db.query(models.Post, func.count(models.Votes.post_id).label("votes")) \
        .join(models.Votes, models.Votes.post_id == models.Post.id, isouter=True) \
        .group_by(models.Post.id) \
        .filter(models.Post.title.contains(search)) \
        .order_by(models.Post.created_at.desc()) \
        .limit(limit) \
        .offset(skip) \
        .all()

    return [{"post": post, "votes": votes} for post, votes in posts]

@router.post("/",status_code=status.HTTP_201_CREATED,response_model= schemas.Post)
def create_posts(post: schemas.PostCreate, db: Session = Depends(get_dp), current_user: int = Depends(oauth2.get_current_user)):
    

    
    new_post = models.Post(owner_id= current_user.id, **post.dict())
    db.add(new_post)
    db.commit()
    db.refresh(new_post)
    return new_post

@router.get("/{id}", response_model=schemas.PostOut)
def get_post(id: int, response: Response, db: Session = Depends(get_dp), response_model=schemas.Post, current_user: int = Depends(oauth2.get_current_user)):


    post = db.query(models.Post, func.count(models.Votes.post_id).label("votes")).join(
        models.Votes, models.Votes.post_id == models.Post.id, isouter=True
    ).group_by(models.Post.id).filter(models.Post.id == id).first()



    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Post with id: {id} was not found")
    
    return {"post": post[0], "votes": post[1]}


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(id: int, db: Session = Depends(get_dp), current_user: int = Depends(oauth2.get_current_user)):

    post_query = db.query(models.Post).filter(models.Post.id == id)
    post = post_query.first()

    if post == None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Post with id: {id} was not found")

    if post.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorize to perform requested action")

    delete_s3_object(post.image_url)
    post_query.delete(synchronize_session=False)
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{id}", response_model=schemas.Post)
def update_post(id: int,updated_post: schemas.PostCreate, db: Session = Depends(get_dp), current_user: int = Depends(oauth2.get_current_user)):
    
#    curser.execute("""UPDATE posts SET title = %s, content = %s, published = %s WHERE id = %s RETURNING * """, (post.title, post.content, post.published, str(id)))
#    updated_post = curser.fetchone()
#    conn.commit()
    post_query = db.query(models.Post).filter(models.Post.id == id)
    post = post_query.first()
    if post == None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail= f"Post with id: {id} was not found")
    

    if post.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Not authorize to perform requested action")
    
    post_query.update(updated_post.dict(),synchronize_session = False)
    db.commit()

    return post_query.first()