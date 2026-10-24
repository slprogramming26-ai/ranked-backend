"""Prueft POST /users/delete (Konto loeschen per E-Mail + Passwort, auch fuer Gesperrte)
und dass DELETE /users/ nach dem Umbau auf _delete_user_data noch dasselbe tut —
gegen die ECHTE DB, komplett in einer Transaktion, die am Ende zurueckgerollt wird
(gleicher Trick wie unban_visibility_test.py). Das commit() in den Routen gibt dabei
nur einen Savepoint frei.

Die Test-User werden INNERHALB der Transaktion frisch angelegt (ohne Bilder). Die
S3-Loeschfunktionen sind trotzdem stillgelegt: S3 rollt kein Rollback zurueck.

Start:  .\\venv\\Scripts\\python.exe scripts\\banned_account_delete_test.py
"""
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models, schemas, utils  # noqa: E402
from app.database import engine  # noqa: E402
from app.routers import user as user_router  # noqa: E402

# Sicherheitsnetz: echte S3-Aufrufe abklemmen (Modul-Globals -> greift auch in
# delete_archived_image, das die Funktionen erst beim Aufruf nachschlaegt).
user_router.delete_s3_object = lambda url, db: None
user_router.delete_post_image = lambda url, db: None
user_router.delete_story_image = lambda url, db: None

# __wrapped__: am Rate-Limiter-Dekorator vorbei, der braucht einen echten Request
delete_with_pw = user_router.delete_account_with_password.__wrapped__
delete_logged_in = user_router.delete_account

fehler = 0


def check(name, bedingung, info=""):
    global fehler
    print(f"  {'OK ' if bedingung else 'FEHLER'}  {name}  {info}")
    if not bedingung:
        fehler += 1


def erwarte_fehler(name, status_code, fn):
    try:
        fn()
        check(name, False, "kein Fehler geworfen")
    except HTTPException as e:
        check(name, e.status_code == status_code, f"{e.status_code} {e.detail}")


conn = engine.connect()
outer = conn.begin()
db = Session(bind=conn, join_transaction_mode="create_savepoint")

PASSWORT = "test-pw-12345"
PW_HASH = utils.hash_password(PASSWORT)
tag = secrets.token_hex(4)


def neuer_user(name, **extra):
    u = models.User(email=f"del_{name}_{tag}@example.com", username=f"del_{name}_{tag}",
                    passwort=PW_HASH, **extra)
    db.add(u)
    db.flush()
    return u


def existiert(user_id):
    return db.query(models.User.id).filter(models.User.id == user_id).first() is not None


def creds(email, pw=PASSWORT):
    return schemas.UserLogin(email=email, passwort=pw)


try:
    fremd = neuer_user("fremd", email_verified=True)
    fremd_post = models.Post(title="t", content="c", owner_id=fremd.id, vote_count=1)
    db.add(fremd_post)
    db.flush()

    print("\n1) Gesperrter User loescht sich per E-Mail + Passwort")
    gesperrt = neuer_user("gesperrt", email_verified=True,
                          banned_until=datetime.now(timezone.utc) + timedelta(days=7),
                          ban_reason="Testbann")
    # Sein Vote auf einem FREMDEN Post -> vote_count muss danach wieder runter
    db.add(models.Votes(user_id=gesperrt.id, post_id=fremd_post.id))
    fremd_post.vote_count = 2
    db.flush()

    erwarte_fehler("falsches Passwort -> 401", 401,
                   lambda: delete_with_pw(request=None, credentials=creds(gesperrt.email, "falsch-123"), db=db))
    erwarte_fehler("unbekannte E-Mail -> 401", 401,
                   lambda: delete_with_pw(request=None, credentials=creds(f"gibtsnicht_{tag}@example.com"), db=db))
    check("nach Fehlversuchen noch da", existiert(gesperrt.id))

    gesperrt_id = gesperrt.id
    # Gross/Leerzeichen: NormalizedEmail im Schema muss das wie beim Login glaetten
    resp = delete_with_pw(request=None, credentials=creds(f"  {gesperrt.email.upper()} "), db=db)
    check("richtige Daten -> user_id zurueck", resp == {"user_id": gesperrt_id}, resp)
    check("User geloescht (trotz Sperre)", not existiert(gesperrt_id))
    db.refresh(fremd_post)
    check("vote_count auf fremdem Post korrigiert", fremd_post.vote_count == 1, fremd_post.vote_count)
    check("Vote weg (CASCADE)",
          db.query(models.Votes).filter(models.Votes.user_id == gesperrt_id).count() == 0)

    print("\n2) Unbestaetigter User darf sich auch loeschen")
    unbestaetigt = neuer_user("unbest", email_verified=False)
    uid = unbestaetigt.id
    resp = delete_with_pw(request=None, credentials=creds(unbestaetigt.email), db=db)
    check("unbestaetigt -> user_id zurueck", resp == {"user_id": uid}, resp)
    check("User geloescht", not existiert(uid))

    print("\n3) Normaler User per POST /users/delete")
    normal = neuer_user("normal", email_verified=True)
    nid = normal.id
    resp = delete_with_pw(request=None, credentials=creds(normal.email), db=db)
    check("normal -> user_id zurueck", resp == {"user_id": nid}, resp)
    check("User geloescht", not existiert(nid))

    print("\n4) DELETE /users/ (eingeloggt) nach dem Umbau")
    eingeloggt = neuer_user("login", email_verified=True)
    db.add(models.Votes(user_id=eingeloggt.id, post_id=fremd_post.id))
    fremd_post.vote_count = 2
    db.flush()
    lid = eingeloggt.id
    resp = delete_logged_in(current_user=eingeloggt, db=db)
    check("DELETE /users/ -> 204", resp.status_code == 204, resp.status_code)
    check("User geloescht", not existiert(lid))
    db.refresh(fremd_post)
    check("vote_count auf fremdem Post korrigiert", fremd_post.vote_count == 1, fremd_post.vote_count)

    check("Fremder User unberuehrt", existiert(fremd.id))

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
