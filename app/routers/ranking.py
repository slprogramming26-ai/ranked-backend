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
from ..ranking_config import SWIPE_POINTS
from datetime import datetime, timedelta, timezone, date


router = APIRouter(
    prefix="/ranking",
    tags=['Ranking']
)

@router.get("/my_target", response_model=schemas.MyTargetOut)
def get_personal_target(db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):
    today = date.today()
    one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    # 1. Prüfen: Hat der User selbst Ranking aktiviert?
    if not current_user.ranking_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Du musst selbst am Ranking teilnehmen, um andere bewerten zu können."
        )

    # 2. Tagessperre über DailyTarget: existiert heute schon ein Match → idempotent
    #    dasselbe Target zurückgeben (Doppel-Swipe verhindert swipe_session).
    existing_match = db.query(models.DailyTarget).filter(
        models.DailyTarget.voter_id == current_user.id,
        models.DailyTarget.date == today
    ).first()

    if existing_match:
        target = existing_match.target_user
    else:
        # 3. Zufälliges Target finden, das:
        # - Nicht der User selbst ist
        # - Das Ranking-Feature aktiviert hat (Opt-In)
        # - Posts in den letzten 7 Tagen hat
        target = db.query(models.User).filter(
            models.User.id != current_user.id,
            models.User.ranking_enabled == True
        ).join(models.Post, models.Post.owner_id == models.User.id) \
         .filter(models.Post.created_at >= one_week_ago) \
         .group_by(models.User.id) \
         .order_by(func.random()) \
         .first()

        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Aktuell sind keine anderen Teilnehmer für das Ranking verfügbar."
            )

        # 4. Match speichern
        new_match = models.DailyTarget(voter_id=current_user.id, target_user_id=target.id)
        db.add(new_match)
        db.commit()

    # 5. Posts des Targets der letzten 7 Tage laden (inkl. flag fürs Swipen)
    posts = db.query(models.Post).filter(
        models.Post.owner_id == target.id,
        models.Post.created_at >= one_week_ago
    ).order_by(models.Post.created_at.desc()).all()

    return {"user_data": target, "posts": posts}


@router.post("/swipe_session", response_model=schemas.SwipeSessionOut, status_code=status.HTTP_201_CREATED)
def swipe_session(
    session: schemas.SwipeSession,
    db: Session = Depends(get_dp),
    current_user: models.User = Depends(oauth2.get_current_user)
):
    today = date.today()
    daily_target = db.query(models.DailyTarget).filter(
        models.DailyTarget.voter_id == current_user.id,
        models.DailyTarget.date == today,
        models.DailyTarget.target_user_id == session.target_user_id,
        ).first()
    
    if not daily_target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No target found. get yourself a target first")
    
    already_swiped = db.query(models.RankingScores).filter(
        models.RankingScores.voter_id == current_user.id,
        func.date(models.RankingScores.created_at) == today
    ).first()

    if already_swiped:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You already voted tody")
    
    post_ids = [s.post_id for s in session.swipes]
    if len(post_ids) != len(set(post_ids)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You can only swipe once")

    posts = db.query(models.Post).filter(models.Post.id.in_(post_ids)).all()
    posts_by_id = {p.id: p for p in posts}

    if len(posts_by_id) != len(post_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mindestens ein Post existiert nicht.")

    for post in posts_by_id.values():
        if post.owner_id != session.target_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Alle Posts müssen vom Target stammen."
            )
        
    breakdown = {"none": 0, "engagement": 0, "creativity": 0, "productivity": 0}
    total_points = 0
    rows = []

    for s in session.swipes:
        post = posts_by_id[s.post_id]
        side = "right" if s.direction else "left"
        pts = SWIPE_POINTS[post.flag][side]

        total_points += pts
        breakdown[post.flag or "none"] += pts

        rows.append(models.RankingScores(
            voter_id=current_user.id,
            post_id=post.id,
            direction=s.direction,
            points=pts
        ))

    try:
        db.add_all(rows)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not save swipes")

    return {
        "success": True,
        "total_points": total_points,
        "breakdown": breakdown,
        "message": f"+{total_points} Punkte!"
    }
    

@router.get("/leaderboard", response_model=List[schemas.LeaderboardEntry])
def get_leaderboard(db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):
    today = datetime.now(timezone.utc).date()

    # Bewerteter User = Besitzer des geswipten Posts.
    # Weg: RankingScores --post_id--> Post --owner_id--> User
    scores = db.query(
        models.Post.owner_id.label("target_user_id"),
        models.User.username,
        models.User.profile_picture_url,
        func.sum(models.RankingScores.points).label("total_points"),
        func.count(models.RankingScores.id).label("total_ratings")
    ).join(models.Post, models.Post.id == models.RankingScores.post_id) \
     .join(models.User, models.User.id == models.Post.owner_id) \
     .filter(func.date(models.RankingScores.created_at) == today) \
     .group_by(models.Post.owner_id, models.User.username, models.User.profile_picture_url) \
     .order_by(func.sum(models.RankingScores.points).desc()) \
     .limit(7) \
     .all()

    return [
        {
            "target_user_id": row.target_user_id,
            "username": row.username,
            "profile_picture_url": row.profile_picture_url,
            "total_points": row.total_points,
            "total_ratings": row.total_ratings,
        }
        for row in scores
    ]


