from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy.orm import Session
from .. import schemas, database, models, oauth2

router = APIRouter(
    prefix="/report",
    tags=["Report"]
)


def _create_report(db: Session, reporter_id: int, reported_user_id: int, report: schemas.ReportCreate,
                   target_type: str, target_id: int = None, content_snapshot: str = None):
    """Gemeinsamer Teil aller Routen: Selbst-Report abfangen,
    Duplikat prüfen, Report anlegen."""

    if reporter_id == reported_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="you cannot report yourself or your own content")

    # Duplikat-Check: gleicher Melder + gleiches Ziel. Bei User-Reports ist target_id None —
    # der Vergleich mit None wird zu IS NULL, reported_user_id unterscheidet dann die User.
    duplicate = db.query(models.Report).filter(
        models.Report.reporter_id == reporter_id,
        models.Report.reported_user_id == reported_user_id,
        models.Report.target_type == target_type,
        models.Report.target_id == target_id
    ).first()

    if duplicate:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="you have already reported this")

    new_report = models.Report(reporter_id=reporter_id, reported_user_id=reported_user_id,
                               reason=report.reason, details=report.details, target_type=target_type,
                               target_id=target_id, content_snapshot=content_snapshot)
    db.add(new_report)
    db.commit()

    return {"message": "report submitted"}


@router.post("/post/{id}", status_code=status.HTTP_201_CREATED)
def report_post(id: int, report: schemas.ReportCreate, db: Session = Depends(database.get_dp),
                current_user: int = Depends(oauth2.get_current_user)):

    post = db.query(models.Post).filter(models.Post.id == id).first()
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"post with id {id} does not exist")

    return _create_report(db, reporter_id=current_user.id, reported_user_id=post.owner_id,
                          report=report, target_type="post", target_id=post.id,
                          content_snapshot=f"{post.title}\n\n{post.content}")


@router.post("/story/{id}", status_code=status.HTTP_201_CREATED)
def report_story(id: int, report: schemas.ReportCreate, db: Session = Depends(database.get_dp),
                 current_user: int = Depends(oauth2.get_current_user)):

    story = db.query(models.Story).filter(models.Story.id == id).first()
    if not story:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"story with id {id} does not exist")

    return _create_report(db, reporter_id=current_user.id, reported_user_id=story.owner_id,
                          report=report, target_type="story", target_id=story.id)


@router.post("/comment/{id}", status_code=status.HTTP_201_CREATED)
def report_comment(id: int, report: schemas.ReportCreate, db: Session = Depends(database.get_dp),
                   current_user: int = Depends(oauth2.get_current_user)):

    comment = db.query(models.Comments).filter(models.Comments.id == id).first()
    if not comment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"comment with id {id} does not exist")

    return _create_report(db, reporter_id=current_user.id, reported_user_id=comment.user_id,
                          report=report, target_type="comment", target_id=comment.id,
                          content_snapshot=comment.comment)


@router.post("/user/{id}", status_code=status.HTTP_201_CREATED)
def report_user(id: int, report: schemas.ReportCreate, db: Session = Depends(database.get_dp),
                current_user: int = Depends(oauth2.get_current_user)):

    user = db.query(models.User).filter(models.User.id == id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"user with id {id} does not exist")

    return _create_report(db, reporter_id=current_user.id, reported_user_id=user.id,
                          report=report, target_type="user")

