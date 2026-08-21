"""Feed-Impressions entgegennehmen — Trainingsdaten fuer Lytir.

Dieser Endpunkt ist bewusst fire-and-forget: der Client hat kein Retry, liest
den Response-Body nicht und loggt einen Fehlerstatus hoechstens im Debug-Log.
Daraus folgt die Grundhaltung fuer die ganze Datei — lieber eine Zeile weniger
schreiben als den kompletten Batch mit einem Fehler verwerfen. Was hier
abbricht, ist nicht "spaeter nochmal da", sondern endgueltig weg.

Vorlaeufig, nur fuer die Beta-Datensammlung.
"""

from fastapi import Response, status, Depends, APIRouter
from sqlalchemy.orm import Session
from typing import Dict, List, Set, Tuple
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from dataclasses import asdict
from datetime import datetime, timezone
from .. import models, schemas, oauth2
from ..database import get_dp
from ..lytir.features import FEATURE_VERSION, feature_input_from_orm

router = APIRouter(
    prefix="/impressions",
    tags=["Impressions"])


def _merge_duplicate_items(
    items: List[schemas.ImpressionIn],
) -> List[schemas.ImpressionIn]:
    """Mehrfach vorkommende post_ids INNERHALB eines Batches zusammenfassen.

    Warum das sein muss: der Upsert in Teil 2 schreibt den ganzen Batch als EIN
    "INSERT ... ON CONFLICT DO UPDATE". Postgres lehnt so ein Statement komplett
    ab, sobald zwei Zeilen auf dieselbe Konflikt-Zeile zeigen — "ON CONFLICT DO
    UPDATE command cannot affect row a second time". Ein einziger doppelter Post
    im Payload wuerde also den kompletten Batch kippen.

    Zusammengefasst wird nach denselben Regeln wie beim Upsert selbst, damit es
    egal ist, ob zwei Meldungen im selben Batch oder in zwei Batches ankommen:
    dwell_ms -> Maximum, Booleans -> ODER, position/shown_at -> der erste Wert.
    """
    merged: Dict[int, schemas.ImpressionIn] = {}

    for item in items:
        seen = merged.get(item.post_id)
        if seen is None:
            merged[item.post_id] = item
            continue

        merged[item.post_id] = item.model_copy(update={
            # position und shown_at stammen vom ersten Sichtbarwerden und
            # aendern sich nie -> den zuerst gesehenen Wert behalten.
            "position": seen.position,
            "shown_at": seen.shown_at,
            "dwell_ms": max(seen.dwell_ms, item.dwell_ms),
            "voted": seen.voted or item.voted,
            "opened_comments": seen.opened_comments or item.opened_comments,
            "shared": seen.shared or item.shared,
            "reported": seen.reported or item.reported,
        })

    return list(merged.values())


def _load_batch_context(
    db: Session,
    current_user,
    post_ids: Set[int],
) -> Tuple[Dict[int, Tuple[models.Post, models.User]], Set[int], Dict[int, int]]:
    """Alles laden, was der Feature-Snapshot braucht — drei Queries, nicht 3xN.

    Die naive Variante waere, pro Item Post, Autor, Follow-Status und
    Comment-Count einzeln zu holen. Bei einem Batch mit 30 Items sind das ueber
    100 Round-Trips fuer Daten, die kein User je sieht. Also einmal alles auf
    einmal, danach nur noch Dict-Lookups.
    """
    # --- Query 1: Post + Autor in einem Rutsch ---------------------------
    # Der Join zieht den Autor gleich mit; ein spaeteres post.owner waere ein
    # Lazy-Load pro Zeile und damit genau das N+1, das wir vermeiden wollen.
    rows: List[Tuple[models.Post, models.User]] = (
        db.query(models.Post, models.User)
        .join(models.User, models.User.id == models.Post.owner_id)
        .filter(models.Post.id.in_(post_ids))
        .all()
    )
    posts_by_id = {post.id: (post, author) for post, author in rows}

    # --- Query 2: Follow-Status des aufrufenden Users ---------------------
    # Nur die Autoren abfragen, die im Batch wirklich vorkommen.
    author_ids = {author.id for _, author in rows}
    followed_author_ids: Set[int] = set()
    if author_ids:
        followed_author_ids = {
            row.followee_id
            for row in db.query(models.Follows.followee_id).filter(
                models.Follows.follower_id == current_user.id,
                models.Follows.followee_id.in_(author_ids),
            ).all()
        }

    # --- Query 3: Comment-Counts ------------------------------------------
    # GROUP BY liefert nur Posts MIT Kommentaren. Posts ohne fehlen im Dict —
    # deshalb spaeter immer mit .get(post_id, 0) lesen, nie mit [].
    comment_counts: Dict[int, int] = dict(
        db.query(models.Comments.post_id, func.count(models.Comments.id))
        .filter(models.Comments.post_id.in_(post_ids))
        .group_by(models.Comments.post_id)
        .all()
    )

    return posts_by_id, followed_author_ids, comment_counts


@router.post("/", status_code=status.HTTP_204_NO_CONTENT)
def post_impressions(
    impression_batch: schemas.ImpressionBatch,
    db: Session = Depends(get_dp),
    current_user: int = Depends(oauth2.get_current_user),
):
    # Doppelte post_ids im Payload zusammenfassen, bevor irgendetwas anderes
    # passiert — sonst kippt weiter unten das Upsert-Statement.
    items = _merge_duplicate_items(impression_batch.items)

    post_ids = {item.post_id for item in items}
    posts_by_id, followed_author_ids, comment_counts = _load_batch_context(
        db, current_user, post_ids
    )

    # Posts, die es nicht mehr gibt, MUESSEN rausfliegen: feed_impressions.post_id
    # hat einen Fremdschluessel auf posts.id. Eine Zeile fuer einen geloeschten
    # Post wuerde nicht etwa mit NULL-Features durchrutschen, sondern das INSERT
    # mit einem ForeignKeyViolation abbrechen — und damit alle gueltigen Zeilen
    # desselben Batches mitreissen.
    items = [item for item in items if item.post_id in posts_by_id]

    if not items:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # --- Feature-Snapshot ------------------------------------------------
    # EIN now fuer den ganzen Batch: sonst bekaeme das letzte Item ein minimal
    # groesseres age_hours als das erste, obwohl beide im selben Feed standen.
    now = datetime.now(timezone.utc)

    values = []
    for item in items:
        post, author = posts_by_id[item.post_id]

        # Rohwerte, NICHT der fertige Vektor. Bei FEATURE_VERSION = 2 laesst sich
        # build_features() so nachtraeglich ueber die alten Daten laufen — ein
        # gespeicherter Vektor waere ab da wertlos.
        feature_input = feature_input_from_orm(
            post,
            author,
            current_user,
            i_follow_owner=author.id in followed_author_ids,
            comment_count=comment_counts.get(item.post_id, 0),
            now=now,
        )

        values.append({
            "user_id": current_user.id,          # aus dem Token, nie aus dem Body
            "post_id": item.post_id,
            "feed_session_id": impression_batch.feed_session_id,
            "feed_variant": impression_batch.feed_variant,
            "position": item.position,
            "shown_at": item.shown_at,
            "dwell_ms": item.dwell_ms,
            "voted": item.voted,
            "opened_comments": item.opened_comments,
            "shared": item.shared,
            "reported": item.reported,
            "features": asdict(feature_input),
            "feature_version": FEATURE_VERSION,
        })

    # --- Upsert: der ganze Batch in EINEM Statement -----------------------
    stmt = pg_insert(models.FeedImpression).values(values)
    stmt = stmt.on_conflict_do_update(
        # Muss exakt den Spalten von uq_feed_impression_user_session_post
        # entsprechen, sonst findet Postgres den Index nicht.
        index_elements=["user_id", "feed_session_id", "post_id"],
        set_={
            # ORDNUNGSUNABHAENGIG mergen statt last-write-wins: der 10s-Timer und
            # ein Gate-Flush koennen gleichzeitig unterwegs sein, ein aelterer
            # kleinerer dwell_ms kann also NACH einem neueren ankommen. Mit
            # blindem Ueberschreiben wuerde die Verweildauer dann schrumpfen.
            "dwell_ms": func.greatest(
                models.FeedImpression.dwell_ms, stmt.excluded.dwell_ms
            ),
            # Analog fuer die Signale: einmal true, immer true. Ein spaeterer
            # Flush, in dem das Flag fehlt, darf es nicht zuruecksetzen.
            "voted": models.FeedImpression.voted | stmt.excluded.voted,
            "opened_comments": (
                models.FeedImpression.opened_comments
                | stmt.excluded.opened_comments
            ),
            "shared": models.FeedImpression.shared | stmt.excluded.shared,
            "reported": models.FeedImpression.reported | stmt.excluded.reported,
        },
        # BEWUSST NICHT im set_: features, feature_version, position, shown_at,
        # received_at.
        #   features/feature_version -> der erste Flush liegt am naechsten am
        #     tatsaechlichen Anzeigezeitpunkt. Wuerden wir den Snapshot bei jedem
        #     Flush ueberschreiben, stuende am Ende der Zustand der LETZTEN
        #     Aktualisierung drin — genau der Fehler, den der Snapshot verhindert.
        #   position/shown_at -> per Contract vom ersten Sichtbarwerden, unveraenderlich.
        #   received_at -> soll die Erstanlage markieren, nicht den letzten Flush.
    )

    db.execute(stmt)
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)
