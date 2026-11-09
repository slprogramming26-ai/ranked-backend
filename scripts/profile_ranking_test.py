"""Prueft POST /profile_ranking/posts/{post_id} (Post ins Profil-Ranking stellen) gegen die
ECHTE DB, komplett in einer Transaktion, die am Ende zurueckgerollt wird (gleicher Trick wie
daily_target_test.py). Das commit() in der Route gibt dabei nur einen Savepoint frei.

Geprueft werden: die vier Pruefungen und ihre Reihenfolge (404 -> 403 -> 409 -> 400), die
7-Tage-Grenze, die Tagessperre (1 Post pro Tag, gestern belegt = heute frei), dass ein
Fehlschlag nichts veraendert und dass die Sperre nur den eigenen User trifft.

Nicht geprueft: zwei echt gleichzeitige Anfragen. Dafuer braeuchte es zwei Verbindungen mit
echtem Commit auf Prod. Abgesichert ist das durch das UPDATE ... WHERE in einer Anweisung.

Start:  .\\venv\\Scripts\\python.exe scripts\\profile_ranking_test.py
"""
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models, utils  # noqa: E402
from app.database import engine  # noqa: E402
from app.ranking_config import PROFILE_RANKING_DAYS  # noqa: E402
from app.routers import profile_ranking  # noqa: E402

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
HEUTE = JETZT.date()
GESTERN = HEUTE - timedelta(days=1)


def neuer_user(name):
    u = models.User(email=f"pr_{name}_{tag}@example.com", username=f"pr_{name}_{tag}",
                    passwort=PW_HASH, email_verified=True)
    db.add(u)
    db.flush()
    return u


def neuer_post(owner, alter=timedelta(0)):
    p = models.Post(owner_id=owner.id, title="t", content="c", created_at=JETZT - alter)
    db.add(p)
    db.flush()
    return p


def hinzufuegen(post_id, user):
    """Ruft die Route auf. Liefert (201, Antwort) oder (Fehlercode, Fehlertext)."""
    try:
        antwort = profile_ranking.add_post_to_ranking(post_id=post_id, db=db, current_user=user)
        return 201, antwort
    except HTTPException as e:
        return e.status_code, e.detail


def eintrag_von(post):
    """ranking_entered_on frisch aus der DB."""
    db.expire_all()
    return db.query(models.Post.ranking_entered_on).filter(models.Post.id == post.id).scalar()


def sperre_von(user):
    """last_ranking_entry_on frisch aus der DB."""
    db.expire_all()
    return db.query(models.User.last_ranking_entry_on).filter(models.User.id == user.id).scalar()


def sperre_setzen(user, datum):
    db.query(models.User).filter(models.User.id == user.id).update(
        {models.User.last_ranking_entry_on: datum}, synchronize_session=False)
    db.flush()


TEXT_SCHON_DRIN = "Dieser Post ist schon im Ranking."
TEXT_TAGESPLATZ = "Du hast heute schon einen Post ins Ranking gestellt."

try:
    anna = neuer_user("anna")
    ben = neuer_user("ben")

    # ---------------------------------------------------------------
    print("\n[0] Route ist in der App eingebunden")
    from app.main import app  # noqa: E402
    pfade = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
    check("POST /profile_ranking/posts/{post_id} existiert",
          ("/profile_ranking/posts/{post_id}", "POST") in pfade)

    # ---------------------------------------------------------------
    print("\n[1] 404: Post existiert nicht")
    hoechste_id = db.query(models.Post.id).order_by(models.Post.id.desc()).limit(1).scalar() or 0
    code, text = hinzufuegen(hoechste_id + 1000, anna)
    check("404", code == 404, f"({code} {text})")
    check("Sperre unveraendert (leer)", sperre_von(anna) is None)

    # ---------------------------------------------------------------
    print("\n[2] 403: fremder Post")
    bens_post = neuer_post(ben)
    code, text = hinzufuegen(bens_post.id, anna)
    check("403", code == 403, f"({code} {text})")
    check("Bens Post nicht im Ranking", eintrag_von(bens_post) is None)
    check("Annas Sperre unveraendert", sperre_von(anna) is None)
    check("Bens Sperre unveraendert", sperre_von(ben) is None)

    # ---------------------------------------------------------------
    print(f"\n[3] 400: Post aelter als {PROFILE_RANKING_DAYS} Tage")
    zu_alt = neuer_post(anna, timedelta(days=PROFILE_RANKING_DAYS, minutes=1))
    code, text = hinzufuegen(zu_alt.id, anna)
    check("400", code == 400, f"({code} {text})")
    check("Post nicht im Ranking", eintrag_von(zu_alt) is None)
    check("Sperre unveraendert (leer)", sperre_von(anna) is None)

    # ---------------------------------------------------------------
    print(f"\n[4] Grenze: knapp unter {PROFILE_RANKING_DAYS} Tagen geht noch")
    knapp = neuer_post(anna, timedelta(days=PROFILE_RANKING_DAYS) - timedelta(minutes=1))
    code, antwort = hinzufuegen(knapp.id, anna)
    check("201", code == 201, f"({code} {antwort})")
    check("Antwort enthaelt post_id", code == 201 and antwort.get("post_id") == knapp.id)
    check("Antwort enthaelt heutiges Datum",
          code == 201 and antwort.get("ranking_entered_on") == HEUTE)
    check("ranking_entered_on = heute (UTC)", eintrag_von(knapp) == HEUTE)
    check("last_ranking_entry_on = heute (UTC)", sperre_von(anna) == HEUTE)

    # ---------------------------------------------------------------
    print("\n[5] 409: derselbe Post noch einmal")
    code, text = hinzufuegen(knapp.id, anna)
    check("409", code == 409, f"({code} {text})")
    check("Grund: schon im Ranking", text == TEXT_SCHON_DRIN, f"({text})")
    check("ranking_entered_on bleibt heute", eintrag_von(knapp) == HEUTE)

    # ---------------------------------------------------------------
    print("\n[6] 409: zweiter eigener Post am selben Tag (Tagesplatz belegt)")
    zweiter = neuer_post(anna)
    code, text = hinzufuegen(zweiter.id, anna)
    check("409", code == 409, f"({code} {text})")
    check("Grund: Tagesplatz", text == TEXT_TAGESPLATZ, f"({text})")
    check("zweiter Post nicht im Ranking", eintrag_von(zweiter) is None)
    check("Sperre bleibt heute", sperre_von(anna) == HEUTE)

    # ---------------------------------------------------------------
    print("\n[7] Reihenfolge der Pruefungen")
    # fremder Post, der schon im Ranking ist -> 403 vor 409
    code, text = hinzufuegen(knapp.id, ben)
    check("fremd + schon drin -> 403", code == 403, f"({code} {text})")
    # zu alter Post, der schon im Ranking ist -> 409 vor 400
    alt_und_drin = neuer_post(anna, timedelta(days=PROFILE_RANKING_DAYS + 3))
    db.query(models.Post).filter(models.Post.id == alt_und_drin.id).update(
        {models.Post.ranking_entered_on: HEUTE - timedelta(days=3)}, synchronize_session=False)
    db.flush()
    code, text = hinzufuegen(alt_und_drin.id, anna)
    check("schon drin + zu alt -> 409 (schon drin)",
          code == 409 and text == TEXT_SCHON_DRIN, f"({code} {text})")
    # zu alter Post bei belegtem Tagesplatz -> 400 vor 409 Tagesplatz
    code, text = hinzufuegen(zu_alt.id, anna)
    check("zu alt + Tagesplatz belegt -> 400", code == 400, f"({code} {text})")

    # ---------------------------------------------------------------
    print("\n[8] Sperre gilt nur fuer den eigenen User")
    code, antwort = hinzufuegen(bens_post.id, ben)
    check("Ben darf heute trotzdem", code == 201, f"({code} {antwort})")
    check("Bens Post im Ranking", eintrag_von(bens_post) == HEUTE)
    check("Bens Sperre = heute", sperre_von(ben) == HEUTE)
    check("Annas zweiter Post weiter nicht drin", eintrag_von(zweiter) is None)

    # ---------------------------------------------------------------
    print("\n[9] Gestern belegt -> heute wieder frei")
    sperre_setzen(anna, GESTERN)
    code, antwort = hinzufuegen(zweiter.id, anna)
    check("201", code == 201, f"({code} {antwort})")
    check("zweiter Post jetzt im Ranking", eintrag_von(zweiter) == HEUTE)
    check("Sperre springt auf heute", sperre_von(anna) == HEUTE)
    check("erster Post bleibt im Ranking", eintrag_von(knapp) == HEUTE)

    # ---------------------------------------------------------------
    print("\n[10] Sperre in der Zukunft (z. B. Uhrzeit-Sprung) blockiert auch")
    dritter = neuer_post(anna)
    sperre_setzen(anna, HEUTE + timedelta(days=1))
    code, text = hinzufuegen(dritter.id, anna)
    check("409 Tagesplatz", code == 409 and text == TEXT_TAGESPLATZ, f"({code} {text})")
    check("dritter Post nicht im Ranking", eintrag_von(dritter) is None)

finally:
    db.close()
    outer.rollback()
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (zurueckgerollt, nichts gespeichert)")
sys.exit(1 if fehler else 0)
