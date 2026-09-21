"""Feed-Logik: welche Posts in welcher Reihenfolge.

Die HTTP-Seite (Parameter, 400er, Antwort-Format) bleibt in routers/post.py.
Hier liegt alles dahinter: Kandidaten-Query, Reihenfolge-Cache in Redis und
das Lytir-Rerank.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import redis
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session, joinedload

from . import models
from .ranking_config import (
    FEED_AGE_PENALTY_PER_HOUR,
    FEED_CANDIDATE_LIMIT,
    FEED_FOLLOW_BONUS,
    FEED_MAX_AGE_DAYS,
    FEED_ORDER_CACHE_TTL,
    FEED_VIBE_BONUS,
    FEED_VOTE_WEIGHT,
    LYTIR_RERANK_ENABLED,
)
from .redis_client import redis_client


def _feed_cache_key(user_id: int, local: bool) -> str:
    """Ein Key pro (User, Feed-Art). Der Lokal-Feed hat eine andere
    Reihenfolge als der globale, die beiden duerfen sich keinen Eintrag
    teilen. `search` taucht hier nicht auf — Suchen umgehen den Cache ganz.
    """
    art = "local" if local else "global"
    return f"feed_order:{user_id}:{art}"


def _cache_lesen(key: str) -> Optional[List[int]]:
    """Gemerkte Reihenfolge holen, oder None.

    None heisst BEIDES: kein Eintrag da (oder abgelaufen) UND Redis kaputt.
    Der Aufrufer behandelt beide Faelle gleich — neu rechnen. Der Feed muss
    ohne Redis weiterlaufen, deshalb wird der Fehler geschluckt.
    """
    try:
        roh = redis_client.get(key)
    except redis.RedisError as e:
        print(f"Warnung: feed-cache lesen fehlgeschlagen ({key}): {e}")
        return None

    if roh is None:
        return None

    return json.loads(roh)


def _cache_schreiben(key: str, post_ids: List[int]) -> None:
    """Reihenfolge merken. Schreibt IMMER die ganze Liste, nie nur den
    Zuwachs — beim Nachladen haben wir die alte sowieso schon im Speicher.
    """
    try:
        redis_client.set(key, json.dumps(post_ids), ex=FEED_ORDER_CACHE_TTL)
    except redis.RedisError as e:
        print(f"Warnung: feed-cache schreiben fehlgeschlagen ({key}): {e}")


def _kandidaten_holen(db: Session,
                      current_user: models.User,
                      *,
                      limit: int,
                      offset: int,
                      search: Optional[str],
                      local: bool):
    # Stufe B vom Report-System: Posts, die ICH gemeldet habe, sehe ich nicht mehr.
    # Korrelierte Subquery statt Python-Liste: laeuft in DERSELBEN Anweisung mit,
    # spart einen Hin-und-Rueckweg (~14 ms gemessen) und die gemeldeten IDs
    # muessen nie durchs Netz.
    ich_habe_gemeldet = db.query(models.Report).filter(
        models.Report.reporter_id == current_user.id,
        models.Report.post_id == models.Post.id,
    ).exists()
    
    
    # is_liked als Spalte statt eigener Query — gleiches Muster wie oben.
    # Der PK von votes ist (user_id, post_id), also deckt der Index beide
        # Bedingungen ab: ein Index-Treffer pro Zeile, kein Tabellenzugriff.
    ich_habe_gevotet = db.query(models.Votes).filter(
        models.Votes.user_id == current_user.id,
        models.Votes.post_id == models.Post.id,
    ).exists()
    
    
        # Kommentarzahl als korreliertes Sub-Select. Anders als die beiden EXISTS
        # oben liefert das keine Ja/Nein-Antwort, sondern eine ZAHL — deshalb
        # func.count(...) und .scalar_subquery() statt .exists().
        # "Korreliert" heisst wieder: models.Post.id kommt von AUSSEN, Postgres
        # rechnet die Zahl pro Ergebniszeile. comments.post_id hat einen Index,
        # also ist das ein Index-Treffer pro Post, kein Scan.
    kommentar_anzahl = db.query(func.count(models.Comments.id)).filter(
        models.Comments.post_id == models.Post.id,
    ).scalar_subquery()
    
    
    
        # Denormalisierter Zaehler auf posts — kein JOIN/GROUP BY ueber votes mehr.
        # Geschrieben wird er atomar in vote.py, die votes-Tabelle bleibt Quelle der Wahrheit.
    vote_count = models.Post.vote_count
    age_hours = func.extract('epoch', func.now() - models.Post.created_at) / 3600
    i_follow_owner = db.query(models.Follows).filter(
        models.Follows.follower_id == current_user.id,
        models.Follows.followee_id == models.Post.owner_id,
    ).exists()
    
    follow_bonus = case((i_follow_owner, FEED_FOLLOW_BONUS), else_=0)
    my_vibes = [current_user.vibe_factor_1, current_user.vibe_factor_2]
    
    same_vibes = db.query(models.User).filter(
        models.User.id == models.Post.owner_id,
        or_(
            models.User.vibe_factor_1.in_(my_vibes),
            models.User.vibe_factor_2.in_(my_vibes),
        ),
        ).exists()
    
    category_bonus = case((same_vibes, FEED_VIBE_BONUS), else_=0)
    
    score = FEED_VOTE_WEIGHT * vote_count \
            - FEED_AGE_PENALTY_PER_HOUR * age_hours \
            + follow_bonus \
            + category_bonus
    
    
    
    
    
    posts_query = db.query(models.Post) \
    .filter(models.Post.title.contains(search)) \
    .filter(~ich_habe_gemeldet)
    
    
    if local:
        posts_query = posts_query.filter(models.Post.location_id == current_user.location_id)
    
        # Kandidatenmenge begrenzen: nur beim normalen Feed (ohne Suche).
        # Wer aktiv nach einem Titel SUCHT, soll auch alte Posts finden koennen.
    if not search:
        feed_cutoff = datetime.now(timezone.utc) - timedelta(days=FEED_MAX_AGE_DAYS)
        posts_query = posts_query.filter(models.Post.created_at >= feed_cutoff)
    
    zeilen = posts_query \
        .add_columns(
        ich_habe_gevotet.label("is_liked"),
        i_follow_owner.label("i_follow_owner"),
        kommentar_anzahl.label("comment_count"),
    ) \
    .options(joinedload(models.Post.owner), joinedload(models.Post.location)) \
    .order_by(score.desc(), models.Post.created_at.desc()) \
    .limit(limit) \
    .offset(offset) \
    .all()


    return zeilen


def _lytir_scores(zeilen, current_user: models.User) -> List[float]:
    """Ein Score pro Kandidat, in derselben Reihenfolge wie `zeilen`.

    PLATZHALTER bis 6b: jeder Post bekommt 0.0. Weil alle gleich sind,
    aendert _rerank die Reihenfolge nicht — die SQL-Reihenfolge bleibt.

    Bewusst eine Liste fuer ALLE Kandidaten statt ein Aufruf pro Post:
    das Modell rechnet spaeter alle 150 in einem einzigen Durchgang.
    """
    return [0.0] * len(zeilen)


def _rerank(zeilen, current_user: models.User):
    """Kandidaten nach Lytir-Score sortieren, hoechster zuerst."""
    scores = _lytir_scores(zeilen, current_user)

    paare = list(zip(scores, zeilen))
    paare.sort(key=lambda paar: paar[0], reverse=True)

    return [zeile for score, zeile in paare]


def _feed_mit_rerank(db: Session,
                     current_user: models.User,
                     *,
                     limit: int,
                     skip: int,
                     local: bool):
    """Rerank-Weg: Kandidaten holen, mit Lytir umsortieren, Seite ausschneiden.

    Noch OHNE Cache-Lesen (kommt in Block 6) — jede Anfrage rechnet neu,
    schreibt die Reihenfolge aber schon weg.
    """
    kandidaten = _kandidaten_holen(
        db, current_user,
        limit=FEED_CANDIDATE_LIMIT,
        offset=0,
        search="",
        local=local,
    )
    sortiert = _rerank(kandidaten, current_user)

    _cache_schreiben(
        _feed_cache_key(current_user.id, local),
        [post.id for post, *_ in sortiert],
    )

    return sortiert[skip:skip + limit]



def seite_holen(db: Session,
                current_user: models.User,
                *,
                limit: int,
                skip: int,
                search: Optional[str],
                local: bool):
    
    """Der einzige Einstieg von aussen: die Zeilen fuer genau EINE Feed-Seite.

    Alles andere in dieser Datei beginnt mit `_` und ist ein Helfer dafuer.

    Zwei Wege:
    - Rerank (Schalter an, keine Suche): 150 Kandidaten holen, Lytir
      sortiert, Python schneidet die Seite aus.
    - Alt (Schalter aus ODER Suche): SQL sortiert, SQL schneidet.
    """
    if LYTIR_RERANK_ENABLED and not search:
        return _feed_mit_rerank(
            db, current_user,
            limit=limit,
            skip=skip,
            local=local,
        )

    return _kandidaten_holen(
        db, current_user,
        limit=limit,
        offset=skip,
        search=search,
        local=local,
    )

