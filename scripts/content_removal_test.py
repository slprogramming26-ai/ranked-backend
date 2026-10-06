"""Prueft das Entfernen von Inhalten durch Mods (DELETE /admin/posts|comments|stories/{id},
/admin/users/{id}/profile-picture) gegen die ECHTE DB — komplett in einer Transaktion,
die am Ende zurueckgerollt wird (gleicher Trick wie report_queue_test.py).

ACHTUNG S3: S3-Loeschungen rollt keine Transaktion zurueck. Deshalb bekommen alle
Testinhalte Bild-URLs OHNE "/public/<bucket>/" -> delete_s3_object steigt sofort aus,
es wird nie ein echtes Bild angefasst.

Start:  .\\venv\\Scripts\\python.exe scripts\\content_removal_test.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models, schemas  # noqa: E402
from app.database import engine  # noqa: E402
from app.routers.admin import (remove_post, remove_comment, remove_story,  # noqa: E402
                               remove_profile_picture)

FAKE_URL = "https://example.invalid/kein-bucket/test.jpg"  # matcht keinen Bucket-Marker

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

try:
    users = db.query(models.User).filter(models.User.role == "user").order_by(models.User.id).limit(4).all()
    if len(users) < 4:
        sys.exit("Brauche mindestens 4 User mit Rolle 'user'")
    a, b, mod, adm = users
    mod.role, adm.role = "moderator", "admin"

    M = models.ModerationAction

    def letzter_log(action):
        return db.query(M).filter(M.moderator_id == mod.id, M.action == action) \
                 .order_by(M.id.desc()).first()

    # Testbuehne (nur in der Transaktion)
    post = models.Post(title="Boeser Titel", content="Boeser Inhalt", owner_id=a.id, image_url=FAKE_URL)
    story = models.Story(owner_id=a.id, image_url=FAKE_URL)
    adm_post = models.Post(title="Admin-Post", content="x", owner_id=adm.id)
    mod_post = models.Post(title="Mod-Post", content="x", owner_id=mod.id)
    db.add_all([post, story, adm_post, mod_post])
    db.flush()
    comment = models.Comments(user_id=a.id, post_id=adm_post.id, comment="Beleidigung")
    a.profile_picture_url = FAKE_URL
    db.add(comment)
    db.flush()

    R = models.Report
    # In der echten DB kann a schon offene User-Meldungen haben -> Grundstand merken.
    user_meldungen_vorher = db.query(R).filter(R.reported_user_id == a.id, R.target_type == "user",
                                               R.status == "pending").count()

    db.add_all([
        models.Report(reporter_id=b.id, reported_user_id=a.id, reason="spam", target_type="post", target_id=post.id),
        models.Report(reporter_id=adm.id, reported_user_id=a.id, reason="spam", target_type="post", target_id=post.id),
        models.Report(reporter_id=b.id, reported_user_id=a.id, reason="harassment",
                      target_type="comment", target_id=comment.id),
        models.Report(reporter_id=b.id, reported_user_id=a.id, reason="inappropriate",
                      target_type="user"),  # User-Meldung
    ])
    db.flush()
    post_id, comment_id, story_id = post.id, comment.id, story.id

    def meldungen(ttype, tid):
        return db.query(R).filter(R.target_type == ttype, R.target_id == tid).all()

    print("\n1) Post entfernen")
    remove_post(post_id, schemas.ContentRemove(reason="spam", details="Kryptowerbung"), db=db, current_user=mod)
    db.expire_all()
    check("Post weg", db.get(models.Post, post_id) is None)
    rs = meldungen("post", post_id)
    check("Meldungen bleiben stehen (kein CASCADE mehr)", len(rs) == 2, len(rs))
    check("  ... und sind action_taken durch mod", all(
        r.status == "action_taken" and r.resolved_by == mod.id and r.resolved_at is not None for r in rs),
        [r.status for r in rs])
    log = letzter_log("delete_post")
    check("Log: Ziel + Grund", log is not None and log.target_id == post_id
          and log.target_user_id == a.id and log.reason == "spam")
    check("Log: Snapshot", log.content_snapshot == "Boeser Titel\n\nBoeser Inhalt", repr(log.content_snapshot))
    check("Log: extra", log.extra == {"reports": 2, "details": "Kryptowerbung"}, log.extra)

    print("\n2) Kommentar entfernen")
    remove_comment(comment_id, schemas.ContentRemove(reason="harassment"), db=db, current_user=mod)
    db.expire_all()
    check("Kommentar weg", db.get(models.Comments, comment_id) is None)
    rs = meldungen("comment", comment_id)
    check("Kommentar-Meldung bleibt, action_taken", len(rs) == 1 and rs[0].status == "action_taken")
    log = letzter_log("delete_comment")
    check("Log: Snapshot = Kommentartext", log.content_snapshot == "Beleidigung")
    check("Log: extra ohne details", log.extra == {"reports": 1}, log.extra)

    print("\n3) Story entfernen")
    remove_story(story_id, schemas.ContentRemove(reason="inappropriate"), db=db, current_user=mod)
    db.expire_all()
    check("Story weg", db.get(models.Story, story_id) is None)
    log = letzter_log("delete_story")
    check("Log: kein Snapshot", log.content_snapshot is None)
    check("Log: 0 Meldungen", log.extra == {"reports": 0}, log.extra)

    print("\n4) Profilbild entfernen")
    remove_profile_picture(a.id, schemas.ContentRemove(reason="inappropriate"), db=db, current_user=mod)
    db.expire_all()
    check("Profilbild-URL leer", db.get(models.User, a.id).profile_picture_url is None)
    log = letzter_log("delete_profile_picture")
    erwartet = user_meldungen_vorher + 1
    check(f"Log: target_id leer, {erwartet} User-Meldung(en)",
          log.target_id is None and log.extra == {"reports": erwartet}, log.extra)
    offen = db.query(R).filter(R.reported_user_id == a.id, R.target_type == "user",
                               R.status == "pending").count()
    check("User-Meldungen bleiben offen", offen == erwartet, offen)

    print("\n5) Verbotenes")
    vorher = db.query(M).count()
    erwarte_fehler("Mod loescht Admin-Post -> 403", 403,
                   lambda: remove_post(adm_post.id, schemas.ContentRemove(reason="spam"), db=db, current_user=mod))
    check("Admin-Post noch da", db.get(models.Post, adm_post.id) is not None)
    erwarte_fehler("Mod loescht eigenen Post -> 400", 400,
                   lambda: remove_post(mod_post.id, schemas.ContentRemove(reason="spam"), db=db, current_user=mod))
    db.rollback()  # verworfene Versuche: Session wie eine echte Request-Session zuruecksetzen
    check("Keine Log-Zeile bei Ablehnung", db.query(M).count() == vorher)
    erwarte_fehler("Unbekannter Post -> 404", 404,
                   lambda: remove_post(999999999, schemas.ContentRemove(reason="spam"), db=db, current_user=mod))
    erwarte_fehler("Kein Profilbild -> 400", 400,
                   lambda: remove_profile_picture(a.id, schemas.ContentRemove(reason="spam"), db=db, current_user=mod))

    print("\n6) Schema")
    for name, daten in [("Freitext-Grund abgelehnt", {"reason": "Spam"}),
                        ("Grund fehlt abgelehnt", {}),
                        ("Unbekanntes Feld abgelehnt", {"reason": "spam", "foo": 1})]:
        try:
            schemas.ContentRemove(**daten)
            check(name, False)
        except ValidationError:
            check(name, True)

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
