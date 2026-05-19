from fastapi import FastAPI, Response, status, HTTPException, Depends, APIRouter
from sqlalchemy.orm import Session
from typing import List, Optional
from sqlalchemy import func
from .. import models, schemas, oauth2
from ..database import get_dp


router = APIRouter(
    prefix="/follow",
    tags=["Follow"])


@router.post("/")
def post_follow(
    follow: schemas.Follow,
    db: Session = Depends(get_dp), 
              current_user: int = Depends(oauth2.get_current_user),
              
              
              ):
    
    followee = db.query(models.User).filter(models.User.id ==  follow.followee_id).first()

    if not followee:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"user with id {follow.followee.id} does not exist")
    

    follow_query = db.query(models.Follows).filter(models.Follows.followee_id ==   follow.followee_id,  models.Follows.follower_id == current_user.id)
    
    found_follow = follow_query.first()

    if (follow.dir == 1):
        if found_follow:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"user {current_user.id} has already followed on user: {follow.followee_id}")
        
        new_follow = models.Follows(followee_id = follow.followee_id, follower_id = current_user.id)
        db.add(new_follow)
        db.commit()
        db.refresh(new_follow)

        return {"message": "sucessfully followed"}

    else:
        if not found_follow:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"user {current_user.id} doesnt follow user: {follow.followee_id}")
        
        follow_query.delete(synchronize_session = False)
        db.commit()

        return {"message": "sucessfully deleted follow"}


    