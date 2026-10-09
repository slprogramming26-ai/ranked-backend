from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Header, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from typing import Optional
import boto3
from ..config import settings
from .. import models
from ..database import get_dp
from .user import delete_archived_image, SNAPSHOT_RETENTION_DAYS

router = APIRouter(
    prefix="/cleanup",
    tags=['Cleanup']
)

s3_client = boto3.client(
    's3',
    endpoint_url=f'{settings.s3_endpoint}',
    aws_access_key_id=f'{settings.s3_access_key}',
    aws_secret_access_key=f'{settings.s3_secret_key}'
)


@router.delete("/failed_deletions")
def retry_failed_deletions(
    db: Session = Depends(get_dp),
    x_cleanup_secret: Optional[str] = Header(default=None),
):
    """Versucht alle gemerkten, fehlgeschlagenen Bild-Löschungen erneut.
    Wird 1x täglich vom externen Scheduler aufgerufen (wie der Story-Cleanup)."""
    if x_cleanup_secret != settings.story_cleanup_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid cleanup secret")

    rows = db.query(models.FailedImageDeletions).all()

    deleted = 0
    for row in rows:
        try:
            s3_client.delete_object(Bucket=row.bucket, Key=row.s3_key)
        except Exception as e:
            print(f"Warnung: Retry für {row.bucket}/{row.s3_key} fehlgeschlagen: {e}")
            continue  # Zeile bleibt stehen -> nächster Versuch morgen

        db.delete(row)  # Bild ist weg -> Merkzettel-Eintrag erledigt
        deleted += 1

    db.commit()
    return {"deleted": deleted, "remaining": len(rows) - deleted}


@router.delete("/moderation_snapshots")
def clear_moderation_snapshots(
    db: Session = Depends(get_dp),
    x_cleanup_secret: Optional[str] = Header(default=None),
):
    """Leert content_snapshot im Audit-Log und in erledigten Meldungen (die Zeilen
    selbst bleiben stehen). Wird 1x täglich vom externen Scheduler aufgerufen."""
    if x_cleanup_secret != settings.story_cleanup_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid cleanup secret")

    M = models.ModerationAction
    # EIN UPDATE fuer beide Faelle: Frist abgelaufen ODER der betroffene User hat
    # seinen Account geloescht (SET NULL hat target_user_id schon geleert).
    cleared = db.query(M).filter(
        M.content_snapshot.isnot(None),
        or_(M.created_at < func.now() - timedelta(days=SNAPSHOT_RETENTION_DAYS),
            M.target_user_id.is_(None)),
    ).update({M.content_snapshot: None}, synchronize_session=False)

    R = models.Report
    # Nur ERLEDIGTE Meldungen (resolved_at gesetzt): offene brauchen den Snapshot
    # noch fuer die Entscheidung. Account geloescht -> Meldungen sind per CASCADE weg.
    cleared_reports = db.query(R).filter(
        R.content_snapshot.isnot(None),
        R.resolved_at < func.now() - timedelta(days=SNAPSHOT_RETENTION_DAYS),
    ).update({R.content_snapshot: None}, synchronize_session=False)

    db.commit()
    return {"cleared": cleared, "cleared_reports": cleared_reports}


@router.delete("/removed_content")
def clear_removed_content(
    db: Session = Depends(get_dp),
    x_cleanup_secret: Optional[str] = Header(default=None),
):
    """Loescht von Mods entfernte Inhalte endgueltig (Archivzeile + S3-Bild), sobald die
    Widerspruchsfrist um ist. Wird 1x täglich vom externen Scheduler aufgerufen."""
    if x_cleanup_secret != settings.story_cleanup_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid cleanup secret")

    RC = models.RemovedContent
    expired = db.query(RC.id, RC.target_type, RC.image_url).filter(
        RC.removed_at < func.now() - timedelta(days=SNAPSHOT_RETENTION_DAYS),
    ).all()

    # delete_s3_object wirft nie: schlaegt S3 fehl, landet das Bild in failed_image_deletions
    # (im selben commit) und /failed_deletions versucht es morgen erneut.
    for row in expired:
        delete_archived_image(row.target_type, row.image_url, db)

    # Ueber die IDs loeschen, NICHT nochmal ueber das Datum: eine Zeile, die zwischen SELECT
    # und DELETE die Frist ueberschreitet, waere sonst weg, ohne dass ihr Bild geloescht wurde.
    deleted = db.query(RC).filter(RC.id.in_([row.id for row in expired])) \
                .delete(synchronize_session=False)

    db.commit()
    return {"deleted": deleted}
