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
from datetime import datetime, timedelta, timezone, date


router = APIRouter(
    prefix="/ranking",
    tags=['Ranking']
)

@router.get("/my_target", response_model=schemas.UserOut)
def get_personal_target(db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):
    import datetime
    today = datetime.date.today()

    # 1. Prüfen: Hat der User selbst Ranking aktiviert?
    if not current_user.ranking_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Du musst selbst am Ranking teilnehmen, um andere bewerten zu können."
        )

    # 2. Bestehendes Match für heute suchen
    existing_match = db.query(models.DailyTarget).filter(
        models.DailyTarget.voter_id == current_user.id,
        models.DailyTarget.date == today
    ).first()

    if existing_match:
        return existing_match.target_user 

    # 3. Zufälliges Target finden, das:
    # - Nicht der User selbst ist
    # - Das Ranking-Feature aktiviert hat (Opt-In)
    random_target = db.query(models.User).filter(
        models.User.id != current_user.id,
        models.User.ranking_enabled == True
    ).order_by(func.random()).first()

    if not random_target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Aktuell sind keine anderen Teilnehmer für das Ranking verfügbar."
        )
    # 4. Match speichern
    new_match = models.DailyTarget(voter_id=current_user.id, target_user_id=random_target.id)
    db.add(new_match)
    db.commit()

    return random_target


@router.post("/daily_ranking_score", response_model=schemas.RankingScoreOut, status_code=status.HTTP_201_CREATED)
def daily_ranking_score(
    ranking_score: schemas.RankingScoreCreate,
    db: Session = Depends(get_dp), 
    current_user: models.User = Depends(oauth2.get_current_user)
):
    # ranking.py — vor dem Speichern:
    

    
    new_daily_rank = models.RankingScores(
        **ranking_score.dict(exclude={'voter_id'}), 
        voter_id=current_user.id
    )


    db.add(new_daily_rank)
    db.commit() 
    db.refresh(new_daily_rank) 
    
    return new_daily_rank


@router.get("/all_week_posts/{target_user_id}", response_model=List[schemas.PostOut])
def get_all_week_posts(target_user_id: int, db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):

    already_rated = db.query(models.RankingScores).filter(
    models.RankingScores.voter_id == current_user.id,
    models.RankingScores.target_user_id == target_user_id,
    func.date(models.RankingScores.created_at) == date.today()
    ).first()

    if already_rated:
        raise HTTPException(status_code=400, detail="Du hast heute schon bewertet.")
    
    # 1. Zeitfenster berechnen (Jetzt minus 7 Tage)
    one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    # 2. Query aufbauen
    all_week_posts = db.query(models.Post, func.count(models.Votes.post_id).label("vote_count")) \
        .join(models.Votes, models.Votes.post_id == models.Post.id, isouter=True) \
        .group_by(models.Post.id) \
        .filter(
            models.Post.owner_id == target_user_id,         
            models.Post.created_at >= one_week_ago        
        ) \
        .order_by(models.Post.created_at.desc()) \
        .all() 

    return [{"post": post, "votes": votes} for post, votes in all_week_posts]


@router.get("/leaderboard", response_model=List[schemas.LeaderboardEntry])
def get_leaderboard(db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):
    today = datetime.now(timezone.utc).date()

    scores = db.query(
        models.RankingScores.target_user_id,
        models.User.username,
        models.User.profile_picture_url,
        func.avg(models.RankingScores.productivity_rating).label("avg_productivity"),
        func.avg(models.RankingScores.creativity_rating).label("avg_creativity"),
        func.avg(models.RankingScores.engagement_rating).label("avg_engagement"),
        func.count(models.RankingScores.id).label("total_ratings")
    ).join(models.User, models.User.id == models.RankingScores.target_user_id) \
     .filter(func.date(models.RankingScores.created_at) == today) \
     .group_by(models.RankingScores.target_user_id, models.User.username, models.User.profile_picture_url) \
     .order_by(func.avg(
         models.RankingScores.productivity_rating +
         models.RankingScores.creativity_rating +
         models.RankingScores.engagement_rating
     ).desc()) \
     .limit(7) \
     .all()

    return [
        {
            "target_user_id": row.target_user_id,
            "username": row.username,
            "profile_picture_url": row.profile_picture_url,
            "avg_productivity": row.avg_productivity,
            "avg_creativity": row.avg_creativity,
            "avg_engagement": row.avg_engagement,
            "total_ratings": row.total_ratings,
        }
        for row in scores
    ]


