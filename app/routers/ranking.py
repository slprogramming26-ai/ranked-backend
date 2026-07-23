from fastapi import FastAPI, Response, status, HTTPException, Depends, APIRouter, UploadFile, File, Header
from sqlalchemy.orm import Session
from PIL import Image
import io
import boto3
from ..config import settings
from ..redis_client import redis_client
from sqlalchemy import func
from .. import models, schemas, oauth2
from ..database import get_dp
from ..ranking_config import SWIPE_POINTS
from ..xp_config import XP_PER_SESSION, XP_PER_POINT_RECEIVED, STREAK_MILESTONE_BONUS, placement_bonus_for_rank
from datetime import datetime, timedelta, timezone, date, time
from typing import Optional
import random
import time as time_module


router = APIRouter(
    prefix="/ranking",
    tags=['Ranking']
)

def _get_leaderboard_from_sql(db, current_user):

    today = datetime.now(timezone.utc).date()
    day_start, day_end = _day_bounds(today)

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
     .filter(models.RankingScores.created_at >= day_start,
             models.RankingScores.created_at < day_end) \
     .group_by(models.Post.owner_id, models.User.username, models.User.profile_picture_url) \
     .order_by(func.sum(models.RankingScores.points).desc()) \
     .limit(7) \
     .all()

    entries = [
        {
            "target_user_id": row.target_user_id,
            "username": row.username,
            "profile_picture_url": row.profile_picture_url,
            "total_points": row.total_points,
            "total_ratings": row.total_ratings,
        }
        for row in scores
    ]

    # --- "Du"-Zeile (Phase 2) ---
    # Punkte pro User HEUTE als wiederverwendbare Subquery: eine Zeile je bewertetem
    # User mit seiner Punktsumme. Basis fuer Rang UND Abstand nach oben.
    per_user = db.query(
        models.Post.owner_id.label("uid"),
        func.sum(models.RankingScores.points).label("pts"),
    ).join(models.Post, models.Post.id == models.RankingScores.post_id) \
     .filter(models.RankingScores.created_at >= day_start,
             models.RankingScores.created_at < day_end) \
     .group_by(models.Post.owner_id).subquery()

    # Meine heute erhaltenen Punkte (0, wenn mich heute niemand bewertet hat ->
    # dann fehlt meine Zeile in per_user und scalar() liefert None).
    my_points = db.query(per_user.c.pts) \
        .filter(per_user.c.uid == current_user.id).scalar()
    my_points = int(my_points or 0)


    # Rang = Anzahl User mit MEHR Punkten als ich, + 1. (Bei Gleichstand teilt man
    # sich denselben Rang; wer 0 hat, landet hinter allen, die heute Punkte haben.)
    higher_count = db.query(func.count()).select_from(per_user) \
        .filter(per_user.c.pts > my_points).scalar()
    my_rank = int(higher_count) + 1

    # Abstand zum naechsthoeheren: die kleinste Punktzahl, die groesser ist als meine,
    # minus meine. None -> es gibt niemanden ueber mir -> ich bin #1 -> 0.
    next_up = db.query(func.min(per_user.c.pts)) \
        .filter(per_user.c.pts > my_points).scalar()
    points_to_next = int(next_up - my_points) if next_up is not None else 0

    return {
        "entries": entries,
        "me": {
            "my_rank": my_rank,
            "my_points": my_points,
            "points_to_next": points_to_next,
        },
    }
    
def _get_leaderboard_from_redis(db, current_user):
    t0 = time_module.perf_counter()
    today = date.today()
    leaderboard_key = f"leaderboard:{today.isoformat()}"
    day_start, day_end = _day_bounds(today)

    top = redis_client.zrevrange(leaderboard_key, 0, 6, withscores=True)
    top_ids = [int(uid) for uid, _ in top]
    t1 = time_module.perf_counter()

    ratings = db.query(
        models.Post.owner_id.label("uid"),
        models.User.username,
        models.User.profile_picture_url,
        func.count(models.RankingScores.id).label("total_ratings"),
    ).join(models.Post, models.Post.id == models.RankingScores.post_id) \
     .join(models.User, models.User.id == models.Post.owner_id) \
     .filter(models.Post.owner_id.in_(top_ids),
             models.RankingScores.created_at >= day_start,
             models.RankingScores.created_at < day_end) \
     .group_by(models.Post.owner_id, models.User.username, models.User.profile_picture_url) \
     .all()
    t2 = time_module.perf_counter()
    info_by_id = {row.uid: row for row in ratings}

    entries = [
        {
            "target_user_id": int(uid),
            "username": info_by_id[int(uid)].username,
            "profile_picture_url": info_by_id[int(uid)].profile_picture_url,
            "total_points": int(points),
            "total_ratings": info_by_id[int(uid)].total_ratings,
        }
        for uid, points in top
    ]

    my_points_raw = redis_client.zscore(leaderboard_key, str(current_user.id))
    my_points = int(my_points_raw) if my_points_raw is not None else 0

    my_rank_raw = redis_client.zrevrank(leaderboard_key, str(current_user.id))
    if my_rank_raw is not None:
        my_rank = my_rank_raw + 1
    else:
        my_rank = redis_client.zcard(leaderboard_key) + 1

    if my_rank > 1:
        next_up = redis_client.zrevrange(leaderboard_key, my_rank - 2, my_rank - 2, withscores=True)
        points_to_next = int(next_up[0][1]) - my_points
    else:
        points_to_next = 0

    t3 = time_module.perf_counter()
    print(f"[timing] redis_top7={t1-t0:.3f}s  sql_enrich={t2-t1:.3f}s  redis_me={t3-t2:.3f}s  total={t3-t0:.3f}s")

    return {
        "entries": entries,
        "me": {
            "my_rank": my_rank,
            "my_points": my_points,
            "points_to_next": points_to_next,
        },
    }





def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Ein Kalendertag als [start, ende)-Zeitfenster in UTC.

    Ersetzt func.date(created_at) == day: gleiche Zeilen, aber die Spalte bleibt
    "nackt" im Vergleich -> Postgres kann den Index auf created_at nutzen.
    ende ist EXKLUSIV (< statt <=), damit Mitternacht nicht doppelt zaehlt."""
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)

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
        #
        # Nur die IDs der Teilnahmeberechtigten (leichtgewichtig, laeuft ueber Indexe):
        eligible = db.query(models.User.id).filter(
            models.User.id != current_user.id,
            models.User.ranking_enabled == True
        ).join(models.Post, models.Post.owner_id == models.User.id) \
         .filter(models.Post.created_at >= one_week_ago) \
         .group_by(models.User.id)

        eligible_count = eligible.count()
        if eligible_count == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Aktuell sind keine anderen Teilnehmer für das Ranking verfügbar."
            )

        # Zufall OHNE order_by(random()): wir wuerfeln eine POSITION (0..count-1)
        # und springen per offset direkt dorthin. order_by(User.id) macht die
        # Reihenfolge deterministisch, sonst waere "Position 17" nicht wohldefiniert.
        target_id = eligible.order_by(models.User.id) \
            .offset(random.randrange(eligible_count)) \
            .limit(1).scalar()

        target = db.query(models.User).filter(models.User.id == target_id).first()
        if not target:
            # Race-Absicherung: User koennte zwischen count() und Auswahl geloescht
            # worden sein — dann wie "keine Teilnehmer" behandeln.
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
    
    day_start, day_end = _day_bounds(today)
    already_swiped = db.query(models.RankingScores).filter(
        models.RankingScores.voter_id == current_user.id,
        models.RankingScores.created_at >= day_start,
        models.RankingScores.created_at < day_end,
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

    # --- XP & Streak (Phase 1.3) ---
    # 1) Streak des BEWERTERS fortschreiben. last_swipe_date = der TAG der letzten
    #    abgeschlossenen Session. War die gestern -> Streak +1, sonst faengt er wieder
    #    bei 1 an. Ein zweiter Lauf am selben Tag ist durch already_swiped oben schon
    #    ausgeschlossen, deshalb muss der Fall "== heute" hier nicht behandelt werden.
    yesterday = today - timedelta(days=1)
    if current_user.last_swipe_date == yesterday:
        current_user.streak_count += 1
    else:
        current_user.streak_count = 1
    current_user.last_swipe_date = today

    # 2) XP fuer den BEWERTER: Grund-XP fuers Abschliessen + evtl. Streak-Meilenstein
    #    (get liefert 0, wenn der neue Streak-Wert kein Meilenstein 7/30/100 ist).
    xp_gained = XP_PER_SESSION + STREAK_MILESTONE_BONUS.get(current_user.streak_count, 0)
    current_user.xp += xp_gained

    # 3) XP fuer den BEWERTETEN: 1 XP pro erhaltenem Punkt, sofort gutgeschrieben.
    #    daily_target.target_user ist der oben validierte Ziel-User (gleiche Session).
    daily_target.target_user.xp += total_points * XP_PER_POINT_RECEIVED


    # --- Activity fuers Target (Phase 4.2) ---
    # Eine aggregierte Meldung pro Session, nicht pro einzelnem Swipe.
    rows.append(models.Activity(
        user_id=session.target_user_id,
        type="rated",
        payload=total_points,
    ))


    try:
        db.add_all(rows)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not save swipes")
    
    leaderboard_key = f"leaderboard:{today.isoformat()}"
    redis_client.zincrby(leaderboard_key, total_points, str(session.target_user_id))
    redis_client.expire(leaderboard_key, 172800)



    return {
        "success": True,
        "total_points": total_points,
        "breakdown": breakdown,
        "message": f"+{total_points} Punkte!",
        "xp_gained": xp_gained,
        "streak": current_user.streak_count,
    }
    

@router.get("/leaderboard", response_model=schemas.LeaderboardOut)
def get_leaderboard(db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):
    try:
        return _get_leaderboard_from_redis(db, current_user)
    except Exception:
        print(f"[leaderboard] Redis-Pfad fehlgeschlagen, Fallback auf SQL für User {current_user.id}")
        return _get_leaderboard_from_sql(db, current_user)



@router.post("/end_of_day")
def end_of_day_bonus(
    day: Optional[date] = None,
    db: Session = Depends(get_dp),
    x_ranking_job_secret: Optional[str] = Header(default=None),
):
    """Vergibt Platzierungs-Boni fuer einen abgeschlossenen Tag. Wird NICHT von
    Usern aufgerufen, sondern vom externen Scheduler kurz nach Mitternacht.
    Geschuetzt durch Header-Secret, kein Login-Token (wie Story-Cleanup)."""
    if x_ranking_job_secret != settings.ranking_job_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid ranking job secret")

    target_day = day or (datetime.now(timezone.utc).date() - timedelta(days=1))
    day_start, day_end = _day_bounds(target_day)

    already_done = db.query(models.DailyBonusLog).filter(
        models.DailyBonusLog.date == target_day
    ).first()
    if already_done:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Bonus for this day was already processed")

    per_user = db.query(
        models.Post.owner_id.label("uid"),
        func.sum(models.RankingScores.points).label("pts"),
    ).join(models.Post, models.Post.id == models.RankingScores.post_id) \
     .filter(models.RankingScores.created_at >= day_start,
             models.RankingScores.created_at < day_end) \
     .group_by(models.Post.owner_id) \
     .order_by(func.sum(models.RankingScores.points).desc()) \
     .all()

    awarded_count = 0
    for rank, row in enumerate(per_user, start=1):
        bonus = placement_bonus_for_rank(rank)
        if bonus == 0:
            break  # per_user ist nach Punkten absteigend sortiert -> alle weiteren Raenge sind auch 0
        user = db.query(models.User).filter(models.User.id == row.uid).first()
        user.xp += bonus
        awarded_count += 1

    db.add(models.DailyBonusLog(date=target_day, awarded_count=awarded_count))

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not save bonuses")

    return {"awarded": awarded_count, "date": target_day}


@router.get("/activity", response_model=list[schemas.ActivityOut])
def get_activity(db: Session = Depends(get_dp), current_user: models.User = Depends(oauth2.get_current_user)):
    return db.query(models.Activity).filter(
        models.Activity.user_id == current_user.id
    ).order_by(models.Activity.created_at.desc()).limit(30).all()



