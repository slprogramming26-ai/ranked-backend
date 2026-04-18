from fastapi import FastAPI, Response, status, HTTPException, Depends, APIRouter, UploadFile, File
from sqlalchemy.orm import Session
from typing import List, Optional
from PIL import Image
import io
import boto3
from ..config import settings

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




@router.post("/upload")
async def upload_post_image(file: UploadFile = File(...)):
    # 1. Bild validieren (nur JPEGs/PNGs)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Nur Bilder erlaubt!")

    # 2. Bild mit Pillow verkleinern (Lerneffekt: Optimierung!)
    image_data = await file.read()
    img = Image.open(io.BytesIO(image_data))
    
    # Resize auf max 1024px Breite (Proportional)
    img.thumbnail((1024, 1024))
    
    # In Byte-Stream zurückverwandeln
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=85) # Kompression spart Platz!
    buffer.seek(0)

    # 3. Zu Supabase hochladen
    file_name = f"posts/{file.filename}"
    try:
        s3_client.upload_fileobj(buffer, BUCKET_NAME, file_name, ExtraArgs={'ACL': 'public-read', 'ContentType': 'image/jpeg'})
        
        # Die fertige URL für die Datenbank generieren
        url = f"{S3_ENDPOINT}/storage/v1/object/public/{BUCKET_NAME}/{file_name}"
        return {"image_url": url}
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail="Upload fehlgeschlagen")
    







@router.get("/", response_model=List[schemas.PostOut])
def get_posts(db: Session = Depends(get_dp), current_user: int = Depends(oauth2.get_current_user), limit: int = 10, skip: int = 0, search: Optional[str] = ""):

    
    posts = db.query(models.Post, func.count(models.Votes.post_id).label("votes")).join(
        models.Votes, models.Votes.post_id == models.Post.id, isouter=True
    ).group_by(models.Post.id).filter(models.Post.title.contains(search)).limit(limit).offset(skip).all()

    return [{"post": post, "votes": votes} for post, votes in posts]

@router.post("/",status_code=status.HTTP_201_CREATED,response_model= schemas.Post)
def create_posts(post: schemas.PostCreate, db: Session = Depends(get_dp), current_user: int = Depends(oauth2.get_current_user)):
    

    
    new_post = models.Post(owner_id= current_user.id, **post.dict())
    db.add(new_post)
    db.commit()
    db.refresh(new_post)
    return new_post

router.get("/{id}", response_model=schemas.PostOut)
def get_post(id: int, response: Response, db: Session = Depends(get_dp), response_model=schemas.Post, current_user: int = Depends(oauth2.get_current_user)):
#    curser.execute("""SELECT * FROM posts WHERE id = %s""", (str(id),))
#    post = curser.fetchone()


    post = db.query(models.Post, func.count(models.Votes.post_id).label("votes")).join(
        models.Votes, models.Votes.post_id == models.Post.id, isouter=True
    ).group_by(models.Post.id).filter(models.Post.id == id).first()



    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Post with id: {id} was not found")
    
    return {"post": post[0], "votes": post[1]}


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(id: int, db: Session = Depends(get_dp), current_user: int = Depends(oauth2.get_current_user)):

#    curser.execute("""DELETE FROM posts WHERE id = %s RETURNING *""", (str(id),))
 #   deleted_post = curser.fetchone()
 #   conn.commit()
    post_query = db.query(models.Post).filter(models.Post.id == id)

    post = post_query.first()

    if post == None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail= f"Post with id: {id} was not found")
    

    if post.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Not authorize to perform requested action")
    
    post_query.delete(synchronize_session = False)
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