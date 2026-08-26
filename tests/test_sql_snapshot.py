"""Charakterisierungs-Test fuer die SQLAlchemy-Migration 1.4.23 -> 2.0.x.

IDEE
----
Ein normaler Test prueft "ist das Ergebnis richtig?". Dieser hier prueft etwas
anderes: "erzeugt mein Code noch exakt dasselbe SQL wie vorher?".

Dafuer wird das SQL aller Tabellen und der wichtigsten Queries EINMAL in die
Datei sql_snapshot.sql geschrieben (auf der alten Version). Nach der Migration
laeuft der Test erneut und vergleicht. Jede Abweichung wird als Diff angezeigt.

ERWARTUNG nach dem Umstieg auf 2.0: genau EINE Abweichung, naemlich der
NUMERIC-Cast in der age_hours-Division des Feeds. Alles andere muss identisch
bleiben. Taucht mehr auf, wurde etwas uebersehen.

Der Test braucht KEINE Datenbank — SQLAlchemy kann Queries zu SQL kompilieren,
ohne sie auszufuehren.
"""
import difflib
import importlib
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import create_engine, func, or_, case
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app import models
from app.ranking_config import (
    FEED_VOTE_WEIGHT,
    FEED_AGE_PENALTY_PER_HOUR,
    FEED_FOLLOW_BONUS,
    FEED_VIBE_BONUS,
)

DIALECT = postgresql.dialect()
SNAPSHOT = Path(__file__).parent / "sql_snapshot.sql"

# Feste Platzhalterwerte. Sie muessen konstant sein, damit derselbe Code immer
# denselben Text erzeugt — ein datetime.now() wuerde den Snapshot bei jedem Lauf
# aendern und der Vergleich waere wertlos.
USER_ID = 1
CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)
VIBES = ["sport", "musik"]


def _session() -> Session:
    """Session ohne Verbindung. create_engine() baut nur die Konfiguration auf,
    verbunden wird erst beim ersten echten Zugriff — den machen wir nie."""
    engine = create_engine("postgresql://user:pw@localhost:5432/db")
    return Session(bind=engine)


def _sql(element) -> str:
    return str(element.compile(dialect=DIALECT))


# --------------------------------------------------------------------------
# Die einzelnen Bausteine, deren SQL wir festhalten
# --------------------------------------------------------------------------

def _ddl_aller_tabellen() -> list[str]:
    """CREATE TABLE fuer jede Tabelle. Faengt jede unbeabsichtigte Aenderung an
    Spalten, Typen oder Constraints — also jedes Schema-Risiko."""
    teile = []
    for table in sorted(models.Base.metadata.sorted_tables, key=lambda t: t.name):
        teile.append(f"-- TABELLE {table.name}\n{_sql(CreateTable(table))}")
    return teile


def _feed_query(db: Session) -> str:
    """Die Feed-Query aus app/routers/post.py (Zeilen 153-199).

    Die Scoring-Logik ist hier bewusst nachgebaut statt importiert: sie steckt
    mitten in der Endpoint-Funktion und laesst sich nicht einzeln aufrufen.
    Aendert sich post.py, muss diese Stelle mitgezogen werden — deshalb steht
    die Zeilennummer oben.
    """
    vote_count = models.Post.vote_count
    age_hours = func.extract("epoch", func.now() - models.Post.created_at) / 3600

    i_follow_owner = db.query(models.Follows).filter(
        models.Follows.follower_id == USER_ID,
        models.Follows.followee_id == models.Post.owner_id,
    ).exists()
    follow_bonus = case((i_follow_owner, FEED_FOLLOW_BONUS), else_=0)

    same_vibes = db.query(models.User).filter(
        models.User.id == models.Post.owner_id,
        or_(
            models.User.vibe_factor_1.in_(VIBES),
            models.User.vibe_factor_2.in_(VIBES),
        ),
    ).exists()
    category_bonus = case((same_vibes, FEED_VIBE_BONUS), else_=0)

    score = (
        FEED_VOTE_WEIGHT * vote_count
        - FEED_AGE_PENALTY_PER_HOUR * age_hours
        + follow_bonus
        + category_bonus
    )

    q = (
        db.query(models.Post)
        .filter(models.Post.title.contains(""))
        # Nicht-leere Liste: notin_([]) erzeugt in 1.4 eine Warnung und anderes
        # SQL als in 2.0 — das waere Rauschen im Diff, kein echter Befund.
        .filter(models.Post.id.notin_([1, 2]))
        .filter(models.Post.created_at >= CUTOFF)
        .order_by(score.desc(), models.Post.created_at.desc())
        .limit(10)
        .offset(0)
    )
    return _sql(q.statement)


def _impression_upsert() -> str:
    """Der Batch-Upsert aus app/routers/impression.py (Zeilen 178-209).
    Das einzige Core-Statement im Projekt — alles andere laeuft ueber db.query()."""
    stmt = pg_insert(models.FeedImpression).values(
        [{"user_id": USER_ID, "feed_session_id": "s", "post_id": 1, "dwell_ms": 100}]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "feed_session_id", "post_id"],
        set_={
            "dwell_ms": func.greatest(
                models.FeedImpression.dwell_ms, stmt.excluded.dwell_ms
            ),
            "voted": models.FeedImpression.voted | stmt.excluded.voted,
            "opened_comments": (
                models.FeedImpression.opened_comments | stmt.excluded.opened_comments
            ),
            "shared": models.FeedImpression.shared | stmt.excluded.shared,
            "reported": models.FeedImpression.reported | stmt.excluded.reported,
        },
    )
    return _sql(stmt)


def _chat_queries(db: Session) -> list[str]:
    """Die Queries aus app/ws/manager.py. Wichtig, weil dort das
    IntegrityError-Handling auf client_msg_id haengt."""
    blocks = db.query(models.Block).filter(
        (models.Block.blocker_id == USER_ID) | (models.Block.blocked_id == USER_ID)
    )
    epoche = db.query(func.max(models.GroupChatEpoch.key_version)).filter(
        models.GroupChatEpoch.group_chat_id == 1
    )
    dup = db.query(models.Message).filter(
        models.Message.sender_id == USER_ID,
        models.Message.client_msg_id == "abc",
    )
    return [
        f"-- QUERY blocked_user_ids\n{_sql(blocks.statement)}",
        f"-- QUERY aktuelle_epoche\n{_sql(epoche.statement)}",
        f"-- QUERY duplikat_lookup\n{_sql(dup.statement)}",
    ]


def _alles_rendern() -> str:
    db = _session()
    try:
        teile = _ddl_aller_tabellen()
        teile.append(f"-- QUERY feed\n{_feed_query(db)}")
        teile.append(f"-- STATEMENT impression_upsert\n{_impression_upsert()}")
        teile.extend(_chat_queries(db))
    finally:
        db.close()
    return "\n\n".join(teile) + "\n"


# --------------------------------------------------------------------------
# Die eigentlichen Tests
# --------------------------------------------------------------------------

def test_sql_hat_sich_nicht_veraendert():
    """Vergleicht das erzeugte SQL mit dem eingecheckten Snapshot."""
    aktuell = _alles_rendern()

    if not SNAPSHOT.exists():
        SNAPSHOT.write_text(aktuell, encoding="utf-8")
        pytest.skip(
            f"Snapshot neu angelegt ({SNAPSHOT.name}). Bitte pruefen und einchecken, "
            "dann laeuft der Test ab jetzt als echter Vergleich."
        )

    erwartet = SNAPSHOT.read_text(encoding="utf-8")
    if aktuell == erwartet:
        return

    # Bei Abweichung: das neue SQL danebenlegen, damit man es nach bewusster
    # Pruefung uebernehmen kann (Datei umbenennen statt Snapshot blind loeschen).
    neu = SNAPSHOT.with_suffix(".aktuell.sql")
    neu.write_text(aktuell, encoding="utf-8")
    diff = "\n".join(
        difflib.unified_diff(
            erwartet.splitlines(),
            aktuell.splitlines(),
            fromfile="snapshot (erwartet)",
            tofile="aktuell",
            lineterm="",
            n=2,
        )
    )
    pytest.fail(f"Das erzeugte SQL hat sich geaendert:\n\n{diff}\n\nNeu geschrieben: {neu.name}")


def test_division_verhalten_ist_bekannt():
    """Dokumentiert die EINE erwartete Aenderung durch die Migration.

    In 2.0 verhaelt sich `/` wie in Python 3 (echte statt Ganzzahl-Division),
    deshalb castet SQLAlchemy den Divisor auf NUMERIC. Folgenlos, weil
    EXTRACT(epoch ...) in Postgres ohnehin nie Integer liefert.
    """
    age_hours = func.extract("epoch", func.now() - models.Post.created_at) / 3600
    sql = _sql(age_hours)

    if sqlalchemy.__version__.startswith("1."):
        assert "CAST" not in sql, f"unter 1.4 unerwartet ein Cast: {sql}"
    else:
        assert "CAST" in sql and "NUMERIC" in sql, f"unter 2.x fehlt der Cast: {sql}"


def test_database_modul_ohne_deprecation_importierbar():
    """Faengt genau den Punkt, um den es bei der Migration geht.

    Unter 1.4.23 ist `sqlalchemy.ext.declarative.declarative_base` noch normal
    -> Test ist gruen. Unter 2.0 ist derselbe Import veraltet -> Test wird ROT,
    bis der Import in app/database.py auf `sqlalchemy.orm` umgestellt ist.
    """
    import app.database

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        importlib.reload(app.database)
