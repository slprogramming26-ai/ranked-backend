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

from .lytir import ranker
from .lytir.features import features_from_orm



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


def _ich_habe_gemeldet(db: Session, current_user: models.User):
    """Stufe B vom Report-System: Posts, die ICH gemeldet habe, sehe ich nie.

    Korrelierte Subquery statt Python-Liste: laeuft in DERSELBEN Anweisung mit,
    spart einen Hin-und-Rueckweg (~14 ms gemessen) und die gemeldeten IDs
    muessen nie durchs Netz. Der Aufrufer schreibt das `~` davor.
    """
    return db.query(models.Report).filter(
        models.Report.reporter_id == current_user.id,
        models.Report.post_id == models.Post.id,
    ).exists()


def _zusatz_spalten(db: Session, current_user: models.User):
    """Die drei Spalten, die neben dem Post mitkommen — als Ausdruecke.

    Hier entsteht nur die Beschreibung, keine Abfrage. Beide Query-Wege
    (Kandidaten-Fenster und `id IN (...)`) benutzen dieselben drei, damit
    die App aus dem Cache dasselbe bekommt wie aus dem Fenster.
    """
    # Der PK von votes ist (user_id, post_id), also deckt der Index beide
    # Bedingungen ab: ein Index-Treffer pro Zeile, kein Tabellenzugriff.
    ich_habe_gevotet = db.query(models.Votes).filter(
        models.Votes.user_id == current_user.id,
        models.Votes.post_id == models.Post.id,
    ).exists()

    i_follow_owner = db.query(models.Follows).filter(
        models.Follows.follower_id == current_user.id,
        models.Follows.followee_id == models.Post.owner_id,
    ).exists()

    # Anders als die beiden EXISTS liefert das keine Ja/Nein-Antwort, sondern
    # eine ZAHL — deshalb func.count(...) und .scalar_subquery(). comments.post_id
    # hat einen Index, also ein Index-Treffer pro Post, kein Scan.
    kommentar_anzahl = db.query(func.count(models.Comments.id)).filter(
        models.Comments.post_id == models.Post.id,
    ).scalar_subquery()

    return ich_habe_gevotet, i_follow_owner, kommentar_anzahl


def _kandidaten_holen(db: Session,
                      current_user: models.User,
                      *,
                      limit: int,
                      offset: int,
                      search: Optional[str],
                      local: bool):


    
    ich_habe_gemeldet = _ich_habe_gemeldet(db, current_user)
    ich_habe_gevotet, i_follow_owner, kommentar_anzahl = _zusatz_spalten(db, current_user)

    
    
    
        # Denormalisierter Zaehler auf posts — kein JOIN/GROUP BY ueber votes mehr.
        # Geschrieben wird er atomar in vote.py, die votes-Tabelle bleibt Quelle der Wahrheit.
    vote_count = models.Post.vote_count
    age_hours = func.extract('epoch', func.now() - models.Post.created_at) / 3600
    
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

def _posts_nach_ids(db: Session, current_user: models.User, post_ids: List[int]) -> dict:
    """Posts zu einer Liste von IDs holen — der Cache-Treffer-Fall.

    Gibt {post_id: zeile} zurueck, KEINE Liste: `IN (47, 12, 93)` darf die
    Zeilen in beliebiger Folge liefern, die Lytir-Reihenfolge steht in der
    ID-Liste des Aufrufers.

    IDs, zu denen nichts kommt, fehlen im Dict — Post geloescht oder in den
    letzten Minuten von mir gemeldet. Der Aufrufer laesst sie einfach aus.
    """
    # Leere Liste: sofort zurueck. `IN ()` waere eine sinnlose Anweisung,
    # und jede Anweisung kostet ~15 ms Rundweg.
    if not post_ids:
        return {}

    ich_habe_gemeldet = _ich_habe_gemeldet(db, current_user)
    ich_habe_gevotet, i_follow_owner, kommentar_anzahl = _zusatz_spalten(db, current_user)

    zeilen = db.query(models.Post) \
        .filter(models.Post.id.in_(post_ids)) \
        .filter(~ich_habe_gemeldet) \
        .add_columns(
            ich_habe_gevotet.label("is_liked"),
            i_follow_owner.label("i_follow_owner"),
            kommentar_anzahl.label("comment_count"),
        ) \
        .options(joinedload(models.Post.owner), joinedload(models.Post.location)) \
        .all()

    return {zeile[0].id: zeile for zeile in zeilen}



def _lytir_scores(zeilen, current_user: models.User) -> List[float]:
    """Ein Score pro Kandidat, in derselben Reihenfolge wie `zeilen`.

    Bewusst eine Liste fuer ALLE Kandidaten statt ein Aufruf pro Post:
    das Modell rechnet alle 150 in einem Durchgang.

    Ohne Modell (keine lytir.json) bekommt jeder Post 0.0 — _rerank sortiert
    stabil, also bleibt dann einfach die SQL-Reihenfolge.
    """
    # EIN Zeitpunkt fuer alle Kandidaten: sonst waere Post 150 ein paar
    # Mikrosekunden "aelter" gerechnet als Post 1, nur weil er spaeter drankam.
    now = datetime.now(timezone.utc)

    vektoren = [
        features_from_orm(
            post, post.owner, current_user,
            i_follow_owner=folge_ich,
            comment_count=anzahl,
            now=now,
        )
        for post, _is_liked, folge_ich, anzahl in zeilen
    ]

    ergebnis = ranker.scores(vektoren)
    if ergebnis is None:
        return [0.0] * len(zeilen)
    return ergebnis



def _rerank(zeilen, current_user: models.User):
    """Kandidaten nach Lytir-Score sortieren, hoechster zuerst."""
    scores = _lytir_scores(zeilen, current_user)

    paare = list(zip(scores, zeilen))
    paare.sort(key=lambda paar: paar[0], reverse=True)

    return [zeile for score, zeile in paare]


def _reihenfolge_fuellen(db: Session,
                         current_user: models.User,
                         reihenfolge: List[int],
                         *,
                         bis: int,
                         local: bool):
    """Fenster fuer Fenster nachladen, bis mindestens `bis` IDs dastehen.

    Gibt ZWEI Dinge zurueck:
      reihenfolge    — die verlaengerte ID-Liste (die kommt in den Cache)
      frische_zeilen — {post_id: zeile} der dabei geladenen ORM-Objekte

    Das Dict ist der Grund, warum Seite 1 mit kaltem Cache nur EINE Anweisung
    kostet: die Posts sind schon da, _posts_nach_ids muss sie nicht nochmal holen.
    """
    reihenfolge = list(reihenfolge)   # eigene Kopie — die Liste des Aufrufers bleibt heil
    frische_zeilen = {}

    while len(reihenfolge) < bis:
        fenster = _kandidaten_holen(
            db, current_user,
            limit=FEED_CANDIDATE_LIMIT,
            offset=len(reihenfolge),
            search="",
            local=local,
        )
        sortiert = _rerank(fenster, current_user)

        # Dedupe: zwischen zwei Fenstern ist now() gewandert und evtl. sind
        # neue Posts dazugekommen. Ein OFFSET auf verschobener Sortierung kann
        # dieselbe Zeile ein zweites Mal liefern.
        schon_da = set(reihenfolge)
        dazu = [zeile for zeile in sortiert if zeile[0].id not in schon_da]

        # Nichts Neues: weiterfragen wuerde ewig dasselbe liefern. Das kann den
        # Feed frueh beenden — bewusst in Kauf genommen (Entscheidung 2026-09-20).
        if not dazu:
            break

        reihenfolge.extend(zeile[0].id for zeile in dazu)
        for zeile in dazu:
            frische_zeilen[zeile[0].id] = zeile

        # Kurzes Fenster heisst: die DB hat nichts mehr. Ohne diesen Abbruch
        # kostet jedes Feed-Ende eine Anweisung extra, nur um "leer" zu hoeren.
        if len(fenster) < FEED_CANDIDATE_LIMIT:
            break

    return reihenfolge, frische_zeilen



def _feed_mit_rerank(db: Session,
                     current_user: models.User,
                     *,
                     limit: int,
                     skip: int,
                     local: bool):
    """Rerank-Weg: gemerkte Reihenfolge benutzen, notfalls verlaengern.

    Seite 1 mit kaltem Cache: 1 Anweisung (das Kandidaten-Fenster — die Posts
    sind damit schon geladen). Seite 2+ mit warmem Cache: 1 Anweisung (id IN).
    """
    key = _feed_cache_key(current_user.id, local)

    # Cache-Miss und kaputtes Redis sind derselbe Fall: nichts gemerkt.
    # Als leere Liste geschrieben faellt die Fallunterscheidung ganz weg —
    # "nichts gemerkt" ist nur der Extremfall von "zu wenig gemerkt".
    reihenfolge = _cache_lesen(key)
    if reihenfolge is None:
        reihenfolge = []

    frische_zeilen = {}
    if len(reihenfolge) < skip + limit:
        reihenfolge, frische_zeilen = _reihenfolge_fuellen(
            db, current_user, reihenfolge,
            bis=skip + limit,
            local=local,
        )
        # Nur schreiben, wenn wirklich etwas dazugekommen ist. Sonst wuerde
        # jede Anfrage den TTL verlaengern und die Reihenfolge waere fuer einen
        # aktiv scrollenden Nutzer eingefroren — neue Posts kaemen nie.
        if frische_zeilen:
            _cache_schreiben(key, reihenfolge)

    seiten_ids = reihenfolge[skip:skip + limit]

    # Was gerade geladen wurde, nicht nochmal holen. Bei Seite 1 ist das alles,
    # `fehlende` ist leer und _posts_nach_ids macht gar keine Anweisung.
    fehlende = [pid for pid in seiten_ids if pid not in frische_zeilen]
    nachgeladen = _posts_nach_ids(db, current_user, fehlende)

    # Die Reihenfolge kommt aus seiten_ids, nicht aus den Dicts. IDs ohne
    # Zeile (Post geloescht oder inzwischen gemeldet) fallen raus — die Seite
    # ist dann kuerzer als `limit`, das ist so gewollt.
    zeilen = {**frische_zeilen, **nachgeladen}
    return [zeilen[pid] for pid in seiten_ids if pid in zeilen]




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

