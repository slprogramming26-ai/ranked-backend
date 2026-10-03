"""Prueft DELETE /cleanup/moderation_snapshots gegen die ECHTE DB — komplett in
einer Transaktion, die am Ende zurueckgerollt wird (gleicher Trick wie in
report_queue_test.py). Es bleibt nichts in der DB stehen.

Start:  .\\venv\\Scripts\\python.exe scripts\\snapshot_cleanup_test.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import engine  # noqa: E402
from app.routers.cleanup import clear_moderation_snapshots, SNAPSHOT_RETENTION_DAYS  # noqa: E402

fehler = 0


def check(name, bedingung, info=""):
    global fehler
    print(f"  {'OK ' if bedingung else 'FEHLER'}  {name}  {info}")
    if not bedingung:
        fehler += 1


conn = engine.connect()
outer = conn.begin()
db = Session(bind=conn, join_transaction_mode="create_savepoint")

try:
    mod, target = db.query(models.User).order_by(models.User.id).limit(2).all()
    jetzt = datetime.now(timezone.utc)
    alt = jetzt - timedelta(days=SNAPSHOT_RETENTION_DAYS + 1)

    def log(created_at, target_user_id, snapshot):
        row = models.ModerationAction(moderator_id=mod.id, action="delete_post", target_id=1,
                                      target_user_id=target_user_id, content_snapshot=snapshot,
                                      created_at=created_at)
        db.add(row)
        return row

    abgelaufen = log(alt, target.id, "alter Text")
    frisch = log(jetzt, target.id, "frischer Text")
    account_weg = log(jetzt, None, "Text von geloeschtem Account")
    db.flush()

    print("1) Falsches Secret")
    try:
        clear_moderation_snapshots(db=db, x_cleanup_secret="falsch")
        check("401 bei falschem Secret", False)
    except HTTPException as e:
        check("401 bei falschem Secret", e.status_code == 401)

    print("\n2) Aufraeumen")
    out = clear_moderation_snapshots(db=db, x_cleanup_secret=settings.story_cleanup_secret)
    # >= 2: falls in der echten DB schon alte Zeilen liegen, zaehlen die mit
    check("mindestens 2 geleert", out["cleared"] >= 2, out)

    for row in (abgelaufen, frisch, account_weg):
        db.refresh(row)
    check("abgelaufen -> geleert", abgelaufen.content_snapshot is None)
    check("Account geloescht -> geleert", account_weg.content_snapshot is None)
    check("frisch -> bleibt", frisch.content_snapshot == "frischer Text")
    check("Zeilen selbst bleiben", db.query(models.ModerationAction).filter(
        models.ModerationAction.id.in_([abgelaufen.id, account_weg.id])).count() == 2)

    print("\n3) Zweiter Lauf")
    check("nichts mehr zu tun", clear_moderation_snapshots(
        db=db, x_cleanup_secret=settings.story_cleanup_secret)["cleared"] == 0)

finally:
    db.close()
    outer.rollback()
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
