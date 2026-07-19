from fastapi import Response, status, HTTPException, Depends, APIRouter, UploadFile, File, Header
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from PIL import Image
import io
import boto3
from ..config import settings
import uuid
from .. import models, schemas, oauth2
from ..database import get_dp

router = APIRouter(
    prefix="/stories",
    tags=['Stories']
)


S3_ENDPOINT = f'{settings.s3_endpoint}'
S3_ACCESS_KEY = f'{settings.s3_access_key}'
S3_SECRET_KEY = f'{settings.s3_secret_key}'
BUCKET_NAME = 'story_images'

s3_client = boto3.client(
    's3',
    endpoint_url=f"{S3_ENDPOINT}",
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY
)

# Wie lange eine Story sichtbar bleibt, bevor sie als "abgelaufen" gilt.
STORY_LIFETIME = timedelta(days=1)


def delete_s3_object(image_url: str | None, db: Session):
    """Löscht eine Datei aus dem Story-Bucket anhand ihrer öffentlichen URL.
    Schluckt Fehler bewusst (analog zu post.delete_s3_object)."""
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
        failed_image_deletion = models.FailedImageDeletions(bucket = BUCKET_NAME, s3_key = s3_key)
        db.add(failed_image_deletion)
        db.commit()
        print(f"Warnung: Story-Bild konnte nicht gelöscht werden: {e}")


def _process_and_upload_image(contents: bytes) -> str:
    """BLOCKIERENDER Teil: Pillow-Verarbeitung (CPU) + S3-Upload (sync I/O).
    Läuft im Threadpool, damit der Event-Loop nicht einfriert.
    Gibt die öffentliche URL des Bildes zurück."""
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
    file_name = f"stories/{uuid.uuid4().hex}.jpg"
    s3_client.upload_fileobj(
        buffer,
        BUCKET_NAME,
        file_name,
        ExtraArgs={'ACL': 'public-read', 'ContentType': 'image/jpeg'}
    )

    return f"{settings.supabase_url}/storage/v1/object/public/{BUCKET_NAME}/{file_name}"


@router.post("/upload", status_code=status.HTTP_201_CREATED, response_model=schemas.StoryOut)
async def upload_story_image(
    file: UploadFile = File(...),
    db: Session = Depends(get_dp),
    current_user: int = Depends(oauth2.get_current_user)
):
    # 1. Dateigröße prüfen (max 15MB)
    MAX_SIZE = 15 * 1024 * 1024
    contents = await file.read(MAX_SIZE + 1)
    if len(contents) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="Datei zu groß! Max 15MB.")

    # 2. Content-Type prüfen
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Nur Bilder erlaubt!")

    # 3. Den schweren Teil im Threadpool ausführen -> Event-Loop bleibt frei.
    try:
        url = await run_in_threadpool(_process_and_upload_image, contents)
    except HTTPException:
        raise  # echte Validierungsfehler (z.B. zu viele Pixel) durchlassen
    except Exception as e:
        print(f"Fehler beim Story-Upload: {e}")
        raise HTTPException(status_code=500, detail="Bildverarbeitung fehlgeschlagen.")

    # 4. Story-Zeile anlegen. created_at setzt die DB selbst (server_default now()).
    new_story = models.Story(owner_id=current_user.id, image_url=url)
    db.add(new_story)
    db.commit()
    db.refresh(new_story)

    return {
        "id": new_story.id,
        "owner_id": new_story.owner_id,
        "image_url": new_story.image_url,
        "created_at": new_story.created_at,
        "owner": new_story.owner,
        "is_mine": True,
    }


@router.get("/", response_model=List[schemas.StoryOut])
def get_stories(
    db: Session = Depends(get_dp),
    current_user: int = Depends(oauth2.get_current_user)
):
    # 1. Wem folgt der aktuelle User? -> Liste der followee_ids
    followee_ids = [
        row.followee_id
        for row in db.query(models.Follows.followee_id).filter(
            models.Follows.follower_id == current_user.id
        ).all()
    ]
    # Sich selbst dazunehmen, damit man die eigenen Stories auch sieht.
    visible_owner_ids = followee_ids + [current_user.id]

    # 2. Nur Stories, die jünger als STORY_LIFETIME (1 Tag) sind.
    cutoff = datetime.now(timezone.utc) - STORY_LIFETIME

    # 3. Stufe B vom Report-System: Stories, die ICH gemeldet habe, sehe ich nicht mehr.
    reported_story_ids = [
        row.story_id
        for row in db.query(models.Report.story_id).filter(
            models.Report.reporter_id == current_user.id,
            models.Report.story_id.isnot(None),
        ).all()
    ]

    stories = db.query(models.Story).filter(
        models.Story.owner_id.in_(visible_owner_ids),
        models.Story.created_at >= cutoff,
        models.Story.id.notin_(reported_story_ids),
    ).order_by(models.Story.created_at.desc()).all()

    return [
        {
            "id": story.id,
            "owner_id": story.owner_id,
            "image_url": story.image_url,
            "created_at": story.created_at,
            "owner": story.owner,
            "is_mine": story.owner_id == current_user.id,
        }
        for story in stories
    ]


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_story(
    id: int,
    db: Session = Depends(get_dp),
    current_user: int = Depends(oauth2.get_current_user)
):
    story_query = db.query(models.Story).filter(models.Story.id == id)
    story = story_query.first()

    if story is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Story with id: {id} was not found")

    if story.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorize to perform requested action")

    delete_s3_object(story.image_url, db)
    story_query.delete(synchronize_session=False)
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/cleanup/expired")
def cleanup_expired_stories(
    db: Session = Depends(get_dp),
    x_cleanup_secret: Optional[str] = Header(default=None),
):
    """Löscht alle Stories, die älter als STORY_LIFETIME (1 Tag) sind - inkl. Bild
    aus dem Storage. Wird NICHT von Usern aufgerufen, sondern von einem externen
    Scheduler (z.B. Supabase pg_cron / cron-job.org) einmal pro Tag.
    Geschützt durch ein geheimes Header-Secret, kein Login-Token."""
    if x_cleanup_secret != settings.story_cleanup_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid cleanup secret")

    cutoff = datetime.now(timezone.utc) - STORY_LIFETIME

    expired = db.query(models.Story).filter(models.Story.created_at < cutoff).all()

    # Erst die Bilder aus dem Bucket löschen (DSGVO: Daten müssen wirklich weg).
    for story in expired:
        delete_s3_object(story.image_url, db)

    # Dann die DB-Zeilen.
    deleted_count = db.query(models.Story).filter(
        models.Story.created_at < cutoff
    ).delete(synchronize_session=False)
    db.commit()

    return {"deleted": deleted_count}
