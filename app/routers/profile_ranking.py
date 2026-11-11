from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import and_, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from .. import models, oauth2, schemas
from ..database import get_dp
from ..feed import _ich_habe_gemeldet
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


@router.get("/users/{user_id}/open", response_model=schemas.ProfileRankingOpenOut)
def open_profile_posts(user_id: int, db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):
    """Aktive Ranking-Posts eines Profils, die ich noch nicht geswipt habe."""
    if user_id == current_user.id:
        return {"can_swipe": False, "posts": []}

    # Anweisung 1: Besitzer + Follow + Block (beide Richtungen) in EINER Abfrage.
    ich_folge = db.query(models.Follows).filter(
        models.Follows.follower_id == current_user.id,
        models.Follows.followee_id == models.User.id,
    ).exists()
    block_besteht = db.query(models.Block).filter(or_(
        and_(models.Block.blocker_id == current_user.id, models.Block.blocked_id == models.User.id),
        and_(models.Block.blocker_id == models.User.id, models.Block.blocked_id == current_user.id),
    )).exists()

    zeile = db.query(
        models.User.banned_until,
        ich_folge.label("ich_folge"),
        block_besteht.label("block_besteht"),
    ).filter(models.User.id == user_id).first()

    if not zeile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User existiert nicht.")

    gesperrt = zeile.banned_until is not None and zeile.banned_until > datetime.now(timezone.utc)
    if not zeile.ich_folge or zeile.block_besteht or gesperrt:
        return {"can_swipe": False, "posts": []}

    # Anweisung 2: nur wenn ich swipen darf. Höchstens 7 Posts, daher ohne Seiten.
    heute = datetime.now(timezone.utc).date()
    schon_geswipt = db.query(models.ProfileRankingSwipe).filter(
        models.ProfileRankingSwipe.voter_id == current_user.id,
        models.ProfileRankingSwipe.post_id == models.Post.id,
    ).exists()

    posts = (
        db.query(models.Post)
        .options(joinedload(models.Post.owner), joinedload(models.Post.location))
        .filter(
            models.Post.owner_id == user_id,
            # gleiche Grenze wie in /swipes, nur umgedreht: > statt <=
            models.Post.ranking_entered_on > heute - timedelta(days=PROFILE_RANKING_DAYS),
            ~schon_geswipt,
            ~_ich_habe_gemeldet(db, current_user),
        )
        .order_by(models.Post.ranking_entered_on.asc(), models.Post.id.asc())  # was zuerst abläuft, zuerst
        .all()
    )
    return {"can_swipe": True, "posts": posts}


@router.get("/users/{user_id}/posts", response_model=List[schemas.ProfileRankingPostOut])
def profile_ranking_posts(user_id: int,
                          limit: int = Query(10, ge=1, le=50),
                          skip: int = Query(0, ge=0),
                          db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):
    """Ranking-Tab: alle Ranking-Posts eines Profils, meiste Likes zuerst. Nur ansehen."""
    # Anweisung 1 (nicht beim eigenen Profil): gibt es den User + Block in beide Richtungen.
    # Keine Follower-Pflicht und kein Sperr-Filter: wie bei den normalen Profil-Posts.
    if user_id != current_user.id:
        block_besteht = db.query(models.Block).filter(or_(
            and_(models.Block.blocker_id == current_user.id, models.Block.blocked_id == models.User.id),
            and_(models.Block.blocker_id == models.User.id, models.Block.blocked_id == current_user.id),
        )).exists()

        zeile = db.query(
            models.User.id,
            block_besteht.label("block_besteht"),
        ).filter(models.User.id == user_id).first()

        if not zeile:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User existiert nicht.")
        if zeile.block_besteht:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Du kannst dieses Ranking nicht ansehen.")

    # Anweisung 2: Posts + mein Swipe als Spalte (True/False/None, kein Extra-Rundweg).
    mein_swipe = db.query(models.ProfileRankingSwipe.liked).filter(
        models.ProfileRankingSwipe.voter_id == current_user.id,
        models.ProfileRankingSwipe.post_id == models.Post.id,
    ).scalar_subquery()

    zeilen = (
        db.query(models.Post, mein_swipe.label("mein_swipe"))
        .options(joinedload(models.Post.owner), joinedload(models.Post.location))
        .filter(
            models.Post.owner_id == user_id,
            models.Post.ranking_entered_on.isnot(None),
            ~_ich_habe_gemeldet(db, current_user),
        )
        .order_by(
            models.Post.ranking_like_count.desc(),
            models.Post.ranking_entered_on.desc(),
            models.Post.id.desc(),  # eindeutig, damit skip/limit nichts doppelt oder gar nicht liefert
        )
        .offset(skip).limit(limit)
        .all()
    )

    heute = datetime.now(timezone.utc).date()
    frist_start = heute - timedelta(days=PROFILE_RANKING_DAYS)
    return [{
        "post": post,
        "ranking_like_count": post.ranking_like_count,
        "ranking_entered_on": post.ranking_entered_on,
        "is_active": post.ranking_entered_on > frist_start,  # gleiche Grenze wie /open und /swipes
        "my_swipe": swipe,
    } for post, swipe in zeilen]


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



