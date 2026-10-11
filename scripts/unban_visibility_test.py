"""Prueft "Entsperren sichtbar machen" (lifted_action_id, Activity account_unbanned,
lifted_at in GET /users/moderation, 400 bei abgelaufener Sperre) gegen die ECHTE DB —
komplett in einer Transaktion, die am Ende zurueckgerollt wird
(gleicher Trick wie report_queue_test.py).

Start:  .\\venv\\Scripts\\python.exe scripts\\unban_visibility_test.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models, schemas  # noqa: E402
from app.database import engine  # noqa: E402
from app.routers.admin import ban_user, unban_user  # noqa: E402
from app.routers.user import get_my_moderation  # noqa: E402

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

M = models.ModerationAction
A = models.Activity


def neuester_log(user_id, action):
    return (db.query(M).filter(M.target_user_id == user_id, M.action == action)
            .order_by(M.created_at.desc(), M.id.desc()).first())


def meine_bans(user):
    rows = get_my_moderation(limit=200, skip=0, current_user=user, db=db)
    # Durchs Schema schicken wie FastAPI es tut -> prueft auch das neue Feld
    return [schemas.MyModerationActionOut.model_validate(r) for r in rows]


try:
    users = (db.query(models.User).filter(models.User.role == "user", models.User.banned_until.is_(None))
             .order_by(models.User.id).limit(3).all())
    if len(users) < 3:
        sys.exit("Brauche mindestens 3 ungesperrte User mit Rolle 'user'")
    a, b, mod = users
    mod.role = "moderator"
    db.flush()

    print("\n1) Ban -> Unban")
    ban_user(a.id, schemas.BanCreate(days=3, reason="Testbann 1"), db=db, current_user=mod)
    ban1 = neuester_log(a.id, "ban")
    unban_user(a.id, db=db, current_user=mod)
    unban1 = neuester_log(a.id, "unban")
    check("unban-Log zeigt auf den Ban", unban1.extra.get("lifted_action_id") == ban1.id, unban1.extra)
    check("unban-Log hat weiter alte Sperre", "was_banned_until" in unban1.extra
          and unban1.extra.get("ban_reason") == "Testbann 1")

    act = db.query(A).filter(A.user_id == a.id, A.type == "account_unbanned").order_by(A.id.desc()).first()
    check("Activity account_unbanned angelegt", act is not None)
    check("payload = ID des BAN-Logs", act is not None and act.payload == ban1.id, act and act.payload)

    eintraege = {e.id: e for e in meine_bans(a)}
    check("Ban in /users/moderation", ban1.id in eintraege)
    check("lifted_at = Zeitpunkt des unban-Logs",
          eintraege[ban1.id].lifted_at == unban1.created_at, eintraege[ban1.id].lifted_at)

    print("\n2) Zweiter Ban (aufgehoben) + dritter Ban (laeuft)")
    ban_user(a.id, schemas.BanCreate(days=5, reason="Testbann 2"), db=db, current_user=mod)
    ban2 = neuester_log(a.id, "ban")
    unban_user(a.id, db=db, current_user=mod)
    unban2 = neuester_log(a.id, "unban")
    check("zweiter unban zeigt auf ZWEITEN Ban", unban2.extra.get("lifted_action_id") == ban2.id)
    ban_user(a.id, schemas.BanCreate(days=7, reason="Testbann 3"), db=db, current_user=mod)
    ban3 = neuester_log(a.id, "ban")

    eintraege = {e.id: e for e in meine_bans(a)}
    check("Ban 1 bleibt aufgehoben", eintraege[ban1.id].lifted_at is not None)
    check("Ban 2 aufgehoben", eintraege[ban2.id].lifted_at == unban2.created_at)
    check("Ban 3 laeuft -> lifted_at None", eintraege[ban3.id].lifted_at is None)
    check("Nicht-Ban-Eintraege -> lifted_at None",
          all(e.lifted_at is None for e in eintraege.values() if e.action != "ban"))
    anzahl = db.query(A).filter(A.user_id == a.id, A.type == "account_unbanned").count()
    check("genau 2 account_unbanned-Activities", anzahl == 2, anzahl)

    print("\n3) Keine Sperre / abgelaufene Sperre")
    erwarte_fehler("Nie gesperrt -> 400", 400, lambda: unban_user(b.id, db=db, current_user=mod))
    b.banned_until = datetime.now(timezone.utc) - timedelta(days=1)
    b.ban_reason = "laengst vorbei"
    db.flush()
    erwarte_fehler("Abgelaufene Sperre -> 400", 400, lambda: unban_user(b.id, db=db, current_user=mod))
    check("keine Activity bei 400",
          db.query(A).filter(A.user_id == b.id, A.type == "account_unbanned").count() == 0)

    print("\n4) Sperre ohne Ban-Log (direkt in der DB gesetzt)")
    b.banned_until = datetime.now(timezone.utc) + timedelta(days=1)
    db.flush()
    vorher = db.query(A).filter(A.user_id == b.id, A.type == "account_unbanned").count()
    unban_user(b.id, db=db, current_user=mod)
    unban_b = neuester_log(b.id, "unban")
    check("entsperrt trotzdem", db.get(models.User, b.id).banned_until is None)
    check("ohne lifted_action_id", "lifted_action_id" not in unban_b.extra, unban_b.extra)
    check("keine Activity", db.query(A).filter(A.user_id == b.id,
                                              A.type == "account_unbanned").count() == vorher)

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
