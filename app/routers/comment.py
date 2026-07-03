from fastapi import FastAPI, Response, status, HTTPException, Depends, APIRouter
from sqlalchemy.orm import Session
from typing import List, Optional
from sqlalchemy import func
from .. import models, schemas, oauth2
from ..database import get_dp

router = APIRouter(
    prefix="/comment",
    tags=["Comments"])

@router.post("/",status_code=status.HTTP_201_CREATED,response_model= schemas.CommentOut)
def create_comment(comment: schemas.CreateComment,db: Session = Depends(get_dp), current_user: int = Depends(oauth2.get_current_user)):


    new_comment = models.Comments(user_id=current_user.id, post_id= comment.post_id, comment= comment.comment)
    db.add(new_comment)
    db.commit()
    db.refresh(new_comment)
    return {'id': new_comment.id, 'comment': new_comment.comment, 'username': current_user.username, 'post_id': new_comment.post_id}

@router.get("/{id}", response_model=List[schemas.CommentOut])
def get_comments(
    id: int,  
    db: Session = Depends(get_dp), 
    current_user: int = Depends(oauth2.get_current_user)
):
    # Prüfen, ob der Post existiert
    post = db.query(models.Post).filter(models.Post.id == id).first()

    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail=f"Post with id: {id} was not found"
        )

    # Stufe B vom Report-System: Kommentare, die ICH gemeldet habe, sehe ich nicht mehr.
    reported_comment_ids = [
        row.comment_id
        for row in db.query(models.Report.comment_id).filter(
            models.Report.reporter_id == current_user.id,
            models.Report.comment_id.isnot(None),
        ).all()
    ]

    # Kommentare holen
    comments = db.query(models.Comments, models.User.username)\
    .join(models.User, models.User.id == models.Comments.user_id)\
    .filter(models.Comments.post_id == id)\
    .filter(models.Comments.id.notin_(reported_comment_ids))\
    .all()

    return [{"id": comment.id, "comment": comment.comment, "username": username, "post_id": comment.post_id} for comment, username in comments]