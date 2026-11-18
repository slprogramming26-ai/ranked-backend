"""Prueft die zwei Datenschutz-Schalter personalized_feed und impressions_enabled
(DSGVO Art. 21 Widerspruch gegen Profiling, Art. 7 Abs. 3 + Art. 17 Widerruf der
Einwilligung fuer Impressions).

Gegen die ECHTE DB, komplett in einer Transaktion, die am Ende zurueckgerollt wird
(gleicher Trick wie daily_target_test.py). Das commit() in den Routen gibt dabei nur
einen Savepoint frei. Redis wird nicht angefasst: der Rerank-Weg ist durch Attrappen
ersetzt, der Feed-Test laeuft ueber `search` und damit ohne Cache.

  A  Defaults + Ausgabe (GET /users/, fremdes Profil)
  B  Setzen ueber PUT /users/ (inkl. null wird ignoriert)
  C  Impressions: speichern nur mit Einwilligung, Widerruf loescht
  D  Feed: Score mit/ohne Follow- und Vibe-Bonus, Filter bleiben
  E  Feed: welcher Weg in seite_holen genommen wird
  F  Tagesranking: _target_gewicht mit/ohne Boni

Start:  .\\venv\\Scripts\\python.exe scripts\\personalization_toggle_test.py
"""
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import Session  # noqa: E402

from app import feed, models, schemas, utils  # noqa: E402
from app.database import engine  # noqa: E402
from app.ranking_config import TARGET_LOCATION_BONUS, TARGET_VIBE_BONUS  # noqa: E402
from app.routers import impression, ranking, user as user_router  # noqa: E402

fehler = 0


def check(name, bedingung, info=""):
    global fehler
    print(f"  {'OK ' if bedingung else 'FEHLER'}  {name}  {info}")
    if not bedingung:
        fehler += 1


conn = engine.connect()
outer = conn.begin()
db = Session(bind=conn, join_transaction_mode="create_savepoint")

PW_HASH = utils.hash_password("test-pw-12345")
tag = secrets.token_hex(4)
JETZT = datetime.now(timezone.utc)


def neuer_user(name, vibe_1=None, vibe_2=None):
    u = models.User(email=f"pt_{name}_{tag}@example.com", username=f"pt_{name}_{tag}",
                    passwort=PW_HASH, email_verified=True,
                    vibe_factor_1=vibe_1, vibe_factor_2=vibe_2)
    db.add(u)
    db.flush()
    return u


def neuer_post(owner, stunden_alt, votes=0):
    # Der Tag im Titel macht die Posts per `search` eindeutig auffindbar,
    # ohne dass die echten Posts der DB den Test stoeren.
    p = models.Post(owner_id=owner.id, title=f"pt_{tag}_{owner.username}", content="c",
                    vote_count=votes, created_at=JETZT - timedelta(hours=stunden_alt))
    db.add(p)
    db.flush()
    return p


def frisch(obj):
    """Wert aus der DB statt aus der Session lesen (die Routen updaten an der Session vorbei)."""
    db.expire_all()
    return obj


def put(**felder):
    """PUT /users/ als voter. Felder werden EXPLIZIT gesetzt, auch None."""
    return user_router.upgrade_user(user_details=schemas.UserDetails(**felder),
                                    current_user=frisch(voter), db=db)


def impressions_von(u):
    return db.query(models.FeedImpression).filter(models.FeedImpression.user_id == u.id).count()


def impression_direkt(u, post):
    """Eine Impression an der Route vorbei anlegen (fuer 'andere bleiben unberuehrt')."""
    db.add(models.FeedImpression(user_id=u.id, post_id=post.id, feed_session_id=f"s_{tag}",
                                 feed_variant="for_you", position=0, shown_at=JETZT, dwell_ms=2000))
    db.flush()


def impressions_senden(post):
    batch = schemas.ImpressionBatch(
        feed_session_id=f"sess_{tag}", feed_variant="for_you", client_sent_at=JETZT,
        items=[schemas.ImpressionIn(post_id=post.id, position=0, shown_at=JETZT, dwell_ms=3000,
                                    voted=False, opened_comments=False, shared=False, reported=False)])
    return impression.post_impressions(impression_batch=batch, db=db, current_user=frisch(voter))


def feed_ids(personalisiert):
    """Eigene Test-Posts in Feed-Reihenfolge, ueber den Such-Weg (kein Redis, kein Rerank)."""
    voter.personalized_feed = personalisiert
    db.flush()
    zeilen = feed.seite_holen(db, frisch(voter), limit=20, skip=0, search=f"pt_{tag}", local=False)
    return [z[0].id for z in zeilen], {z[0].id: z.i_follow_owner for z in zeilen}


try:
    # ---------------------------------------------------------------------
    # Aufbau
    voter = neuer_user("voter", vibe_1="musik", vibe_2="sport")
    freund = neuer_user("freund", vibe_1="musik")          # ich folge ihm, gleicher Vibe
    fremder = neuer_user("fremder", vibe_1="kochen")       # kein Bezug
    geblockt = neuer_user("geblockt", vibe_1="musik")      # von mir geblockt
    anderer = neuer_user("anderer")                         # hat eigene Impressions

    db.add(models.Follows(follower_id=voter.id, followee_id=freund.id))
    db.add(models.Block(blocker_id=voter.id, blocked_id=geblockt.id))
    db.flush()

    # Personalisiert:   freund  = 0 - 2 + 50 + 20 = 68   vor  fremder = 10 - 0 = 10
    # Unpersonalisiert: freund  = 0 - 2           = -2   hinter fremder = 10
    post_freund = neuer_post(freund, stunden_alt=2, votes=0)
    post_fremder = neuer_post(fremder, stunden_alt=0, votes=10)
    post_geblockt = neuer_post(geblockt, stunden_alt=0, votes=100)

    # ---------------------------------------------------------------------
    print("\nA) Defaults + Ausgabe")
    ich = user_router.get_current_user(current_user=frisch(voter), db=db)
    check("A1 personalized_feed Default True (Opt-out)", ich["personalized_feed"] is True,
          f"-> {ich['personalized_feed']}")
    check("A2 impressions_enabled Default False (Opt-in)", ich["impressions_enabled"] is False,
          f"-> {ich['impressions_enabled']}")

    fremdes_profil = schemas.GetUserOut.model_validate(
        user_router.get_user(id=fremder.id, current_user=frisch(voter), db=db))
    check("A3 fremdes Profil zeigt die Schalter NICHT",
          fremdes_profil.personalized_feed is None and fremdes_profil.impressions_enabled is None)

    # ---------------------------------------------------------------------
    print("\nB) Setzen ueber PUT /users/")
    antwort = put(impressions_enabled=True)
    check("B1 impressions_enabled -> True", frisch(voter).impressions_enabled is True,
          f"updated_fields={antwort['updated_fields']}")

    antwort = put(personalized_feed=None, impressions_enabled=None, ranking_enabled=None)
    check("B2 explizites null -> kein 500, nichts geaendert", antwort["updated_fields"] == [],
          f"updated_fields={antwort['updated_fields']}")
    check("B3 Werte nach null unveraendert",
          frisch(voter).impressions_enabled is True and voter.personalized_feed is True)

    antwort = put(personalized_feed=None, biography="hallo")
    check("B4 null gemischt mit echtem Feld -> nur das echte Feld",
          antwort["updated_fields"] == ["biography"], f"updated_fields={antwort['updated_fields']}")

    # ---------------------------------------------------------------------
    print("\nC) Impressions")
    r = impressions_senden(post_fremder)
    check("C1 mit Einwilligung: 204 + Zeile geschrieben",
          r.status_code == 204 and impressions_von(voter) == 1, f"-> {impressions_von(voter)} Zeilen")

    impression_direkt(anderer, post_fremder)

    put(biography="noch was")
    check("C2 PUT ohne impressions_enabled loescht NICHT", impressions_von(voter) == 1,
          f"-> {impressions_von(voter)} Zeilen")

    put(impressions_enabled=True)
    check("C3 erneut True setzen loescht NICHT", impressions_von(voter) == 1)

    put(impressions_enabled=False)
    check("C4 Widerruf loescht meine Impressions", impressions_von(voter) == 0,
          f"-> {impressions_von(voter)} Zeilen")
    check("C5 Impressions anderer User bleiben", impressions_von(anderer) == 1,
          f"-> {impressions_von(anderer)} Zeilen")

    r = impressions_senden(post_fremder)
    check("C6 ohne Einwilligung: 204, aber nichts geschrieben",
          r.status_code == 204 and impressions_von(voter) == 0, f"-> {impressions_von(voter)} Zeilen")

    # ---------------------------------------------------------------------
    print("\nD) Feed-Reihenfolge")
    ids_an, follow_an = feed_ids(personalisiert=True)
    ids_aus, follow_aus = feed_ids(personalisiert=False)

    check("D1 personalisiert: Freund (Follow+Vibe) vor Fremdem",
          ids_an == [post_freund.id, post_fremder.id], f"-> {ids_an}")
    check("D2 unpersonalisiert: Fremder (mehr Votes) vor Freund",
          ids_aus == [post_fremder.id, post_freund.id], f"-> {ids_aus}")
    check("D3 Block-Filter wirkt in BEIDEN Modi",
          post_geblockt.id not in ids_an and post_geblockt.id not in ids_aus)
    check("D4 i_follow_owner bleibt als Anzeige-Spalte (auch unpersonalisiert)",
          follow_aus.get(post_freund.id) is True and follow_aus.get(post_fremder.id) is False)

    # ---------------------------------------------------------------------
    print("\nE) Weg in seite_holen")
    aufrufe = []
    echt_rerank, echt_kandidaten, echt_schalter = (
        feed._feed_mit_rerank, feed._kandidaten_holen, feed.LYTIR_RERANK_ENABLED)
    feed._feed_mit_rerank = lambda *a, **kw: aufrufe.append("rerank") or []
    feed._kandidaten_holen = lambda *a, **kw: aufrufe.append(("sql", kw["personalisiert"])) or []
    feed.LYTIR_RERANK_ENABLED = True
    try:
        for pers, suche in ((True, ""), (False, ""), (True, "x")):
            aufrufe.clear()
            u = SimpleNamespace(id=voter.id, personalized_feed=pers)
            feed.seite_holen(db, u, limit=10, skip=0, search=suche, local=False)
            erwartet = ["rerank"] if (pers and not suche) else [("sql", pers)]
            check(f"E personalisiert={pers}, search={suche!r} -> {erwartet}", aufrufe == erwartet,
                  f"-> {aufrufe}")
    finally:
        feed._feed_mit_rerank, feed._kandidaten_holen, feed.LYTIR_RERANK_ENABLED = (
            echt_rerank, echt_kandidaten, echt_schalter)

    # ---------------------------------------------------------------------
    print("\nF) Tagesranking-Gewicht")
    kandidat = SimpleNamespace(vibe_factor_1="musik", vibe_factor_2=None, location_id=7,
                               heute_gezogen=0, kuerzlich_mein_target=False)
    ich_an = SimpleNamespace(vibe_factor_1="musik", vibe_factor_2=None, location_id=7,
                             personalized_feed=True)
    ich_aus = SimpleNamespace(**{**vars(ich_an), "personalized_feed": False})

    g_an = ranking._target_gewicht(kandidat, ich_an)
    g_aus = ranking._target_gewicht(kandidat, ich_aus)
    check("F1 personalisiert: Vibe- + Orts-Bonus", g_an == 1 + TARGET_VIBE_BONUS + TARGET_LOCATION_BONUS,
          f"-> {g_an}")
    check("F2 unpersonalisiert: Grundgewicht 1", g_aus == 1, f"-> {g_aus}")

    kandidat.heute_gezogen = 1
    kandidat.kuerzlich_mein_target = True
    g_aus_fair = ranking._target_gewicht(kandidat, ich_aus)
    check("F3 Fairness-Teile wirken auch unpersonalisiert", g_aus_fair < g_aus, f"-> {g_aus_fair}")

finally:
    db.close()
    outer.rollback()
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (alles zurueckgerollt)")
sys.exit(1 if fehler else 0)
