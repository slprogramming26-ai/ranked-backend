from fastapi import APIRouter, Depends, status, HTTPException, Response
from .. import schemas, database, models, oauth2
from sqlalchemy.orm import Session

router = APIRouter(
    prefix="/vote",
    tags=["Vote"]
                   )  


@router.post("/",status_code=status.HTTP_201_CREATED,)
def vote(vote: schemas.Vote, db: Session = Depends(database.get_dp),current_user: int = Depends(oauth2.get_current_user)):

    post= db.query(models.Post).filter(models.Post.id == vote.post_id).first()

    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"post with id {vote.post_id} does not exist")

    vote_query = db.query(models.Votes).filter(models.Votes.post_id == vote.post_id, models.Votes.user_id == current_user.id) 
    found_vote = vote_query.first()
    if (vote.dir == 1):
        if found_vote:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"user {current_user.id} has already vote on post: {vote.post_id}")
        new_vote = models.Votes(post_id = vote.post_id, user_id = current_user.id)
        db.add(new_vote)
        # Denormalisierten Zaehler atomar mitfuehren: SQL-seitiges +1 (kein
        # Read-modify-write in Python -> keine Race Condition bei parallelen Votes).
        # Laeuft im SELBEN commit wie die Vote-Zeile -> beide bleiben konsistent.
        db.query(models.Post).filter(models.Post.id == vote.post_id).update(
            {models.Post.vote_count: models.Post.vote_count + 1},
            synchronize_session=False,
        )
        db.commit()
        return {"message": "Sucessfully added vote"}
    
    else: 
        if not found_vote:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"user {current_user.id} did not vote on post: {vote.post_id}")
        
        vote_query.delete(synchronize_session = False)
        # Gegenstueck zum Upvote: atomar -1 im selben commit wie das Loeschen.
        db.query(models.Post).filter(models.Post.id == vote.post_id).update(
            {models.Post.vote_count: models.Post.vote_count - 1},
            synchronize_session=False,
        )
        db.commit()

        return {"message":"Sucessfully deleted vote"}