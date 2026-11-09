from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from .. import models, oauth2, schemas
from ..database import get_dp
from ..ranking_config import PROFILE_RANKING_DAYS

router = APIRouter(prefix="/profile_ranking", tags=["Profile Ranking"])


@router.post("/posts/{post_id}", status_code=status.HTTP_201_CREATED)
def add_post_to_ranking(post_id: int, db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):
    post = db.query(models.Post).filter(models.Post.id == post_id).first()

    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post existiert nicht.")

    if post.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Du kannst nur eigene Posts hinzufügen.")

    if post.ranking_entered_on is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Dieser Post ist schon im Ranking.")

    if post.created_at < datetime.now(timezone.utc) - timedelta(days=PROFILE_RANKING_DAYS):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dieser Post ist zu alt für das Ranking.")

    heute = datetime.now(timezone.utc).date()

    platz_frei = db.query(models.User).filter(
        models.User.id == current_user.id,
        or_(
            models.User.last_ranking_entry_on.is_(None),
            models.User.last_ranking_entry_on < heute,
        ),
    ).update({models.User.last_ranking_entry_on: heute}, synchronize_session=False)

    if platz_frei == 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Du hast heute schon einen Post ins Ranking gestellt.")

    post.ranking_entered_on = heute
    db.commit()
    return {"post_id": post.id, "ranking_entered_on": heute}

def _post_mit_pruefungen(db, current_user, post_id):
    """Post plus Follow- und Block-Prüfung in EINER Anweisung."""
    ich_folge = db.query(models.Follows).filter(
        models.Follows.follower_id == current_user.id,
        models.Follows.followee_id == models.Post.owner_id,
    ).exists()

    # Block in BEIDE Richtungen.
    block_besteht = db.query(models.Block).filter(or_(
        and_(models.Block.blocker_id == current_user.id, models.Block.blocked_id == models.Post.owner_id),
        and_(models.Block.blocker_id == models.Post.owner_id, models.Block.blocked_id == current_user.id),
    )).exists()

    return db.query(
        models.Post,
        ich_folge.label("ich_folge"),
        block_besteht.label("block_besteht"),
    ).filter(models.Post.id == post_id).first()

    

    

@router.post("/swipes", status_code=status.HTTP_201_CREATED)
def swipe_profile_post(swipe: schemas.SwipeIn, db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):
    zeile = _post_mit_pruefungen(db, current_user, swipe.post_id)

    if not zeile or zeile.Post.ranking_entered_on is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post ist nicht im Ranking.")

    post = zeile.Post

    if post.owner_id == current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Eigene Posts kannst du nicht bewerten.")

    if not zeile.ich_folge:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Du musst dem Besitzer folgen, um zu bewerten.")

    if zeile.block_besteht:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Du kannst diesen Post nicht bewerten.")

    heute = datetime.now(timezone.utc).date()
    if post.ranking_entered_on <= heute - timedelta(days=PROFILE_RANKING_DAYS):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Die Bewertungszeit für diesen Post ist abgelaufen.")

    stmt = (
        pg_insert(models.ProfileRankingSwipe)
        .values(voter_id=current_user.id, post_id=swipe.post_id, liked=swipe.direction)
        .on_conflict_do_nothing(index_elements=["voter_id", "post_id"])
        .returning(models.ProfileRankingSwipe.post_id)
    )
    eingefuegt = db.execute(stmt).scalar()

    if eingefuegt is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Diesen Post hast du schon bewertet.")

    if swipe.direction:
        db.query(models.Post).filter(models.Post.id == swipe.post_id).update(
            {models.Post.ranking_like_count: models.Post.ranking_like_count + 1},
            synchronize_session=False,
        )

    db.commit()
    return {"post_id": swipe.post_id, "liked": swipe.direction}