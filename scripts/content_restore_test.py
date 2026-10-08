"""Prueft POST /admin/actions/{id}/restore (Phase 3 removed_content): entfernen -> wiederherstellen
-> alles wieder da, mit denselben IDs. Dazu die Sonderfaelle (geloeschter Voter, geloeschter Ort,
409er, doppelter Restore). Laeuft gegen die ECHTE DB, komplett in einer Transaktion, die am Ende
zurueckgerollt wird (gleiches Muster wie content_archive_test.py).

S3: die drei delete_s3_object-Funktionen sind durch Spione ersetzt (vor dem admin-Import).
Weder Entfernen noch Wiederherstellen darf sie aufrufen.

Start:  .\\venv\\Scripts\\python.exe scripts\\content_restore_test.py
"""
import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models, schemas  # noqa: E402
from app.database import engine  # noqa: E402
from app.routers import post as post_router, story as story_router, user as user_router  # noqa: E402

s3_aufrufe = []
post_router.delete_s3_object = lambda url, db: s3_aufrufe.append(("post", url))
story_router.delete_s3_object = lambda url, db: s3_aufrufe.append(("story", url))
user_router.delete_s3_object = lambda url, db: s3_aufrufe.append(("user", url))

from app.routers import admin  # noqa: E402  (erst NACH den Spionen)

FAKE_URL = "https://example.invalid/kein-bucket/test.jpg"
GRUND = schemas.ContentRemove(reason="spam")

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


def spalten(obj):
    """Alle Spaltenwerte einer ORM-Zeile als dict (zum Vorher/Nachher-Vergleich)."""
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


conn = engine.connect()
outer = conn.begin()
db = Session(bind=conn, join_transaction_mode="create_savepoint")

try:
    users = db.query(models.User).filter(models.User.role == "user").order_by(models.User.id).limit(4).all()
    if len(users) < 4:
        sys.exit("Brauche mindestens 4 User mit Rolle 'user'")
    a, b, mod, adm = users
    mod.role, adm.role = "moderator", "admin"

    # Wegwerf-User (wird spaeter geloescht) und Wegwerf-Ort (ebenso)
    tmp = models.User(email="restore-test@example.invalid", passwort="x", username="restore_test_tmp")
    loc = models.Location(name="Restore-Test-Ort")
    db.add_all([tmp, loc])
    db.flush()

    M = models.ModerationAction
    RC = models.RemovedContent

    def letzte_action_id(action_name):
        return db.query(M.id).filter(M.moderator_id == mod.id, M.action == action_name) \
                 .order_by(M.id.desc()).first()[0]

    def restore(action_id, als=None):
        out = admin.restore_content(action_id, db=db, current_user=als or adm)
        db.expire_all()
        return out

    def archiv_da(action_id):
        return db.query(RC).filter(RC.moderation_action_id == action_id).count() == 1

    # ------------------------------------------------------------------
    print("\n1) Post komplett zurueck (nichts hat sich geaendert)")
    pa = models.Post(title="Post A", content="Inhalt A", owner_id=a.id, image_url=FAKE_URL,
                     flag="engagement", vote_count=2)
    db.add(pa)
    db.flush()
    pa_comments = [models.Comments(user_id=u.id, post_id=pa.id, comment=f"A von {u.id}") for u in (b, adm)]
    pa_scores = [models.RankingScores(voter_id=b.id, post_id=pa.id, direction=True, points=4)]
    db.add_all(pa_comments + pa_scores
               + [models.Votes(user_id=u.id, post_id=pa.id) for u in (b, adm)])
    db.flush()
    db.refresh(pa)  # created_at aus der DB holen, damit der Vergleich alle Spalten hat
    pa_id = pa.id
    pa_vorher = spalten(pa)
    pa_comments_vorher = {c.id: spalten(c) for c in pa_comments}
    pa_scores_vorher = {r.id: spalten(r) for r in pa_scores}

    admin.remove_post(pa_id, GRUND, db=db, current_user=mod)
    db.expire_all()
    remove_id = letzte_action_id("delete_post")
    check("Post nach Entfernen weg", db.get(models.Post, pa_id) is None)

    out = restore(remove_id)
    check("Antwort = neue Log-Zeile restore_content",
          out.action == "restore_content" and out.moderator_id == adm.id and out.target_user_id == a.id
          and out.target_id == pa_id, (out.action, out.target_id))
    check("extra = nur restored_action_id", out.extra == {"restored_action_id": remove_id}, out.extra)
    check("Antwort passt ins Schema", schemas.ModerationActionOut.model_validate(out).id == out.id)

    pa_neu = db.get(models.Post, pa_id)
    check("Post wieder da, gleiche ID, ALLE Spalten gleich",
          pa_neu is not None and spalten(pa_neu) == pa_vorher,
          None if pa_neu is None else {k: (v, spalten(pa_neu)[k]) for k, v in pa_vorher.items()
                                       if spalten(pa_neu)[k] != v})
    comments_neu = {c.id: spalten(c) for c in db.query(models.Comments).filter(models.Comments.post_id == pa_id)}
    check("Kommentare: gleiche IDs, gleicher Inhalt", comments_neu == pa_comments_vorher)
    voter_neu = {v.user_id for v in db.query(models.Votes).filter(models.Votes.post_id == pa_id)}
    check("Votes zurueck", voter_neu == {b.id, adm.id}, voter_neu)
    scores_neu = {r.id: spalten(r) for r in db.query(models.RankingScores).filter(models.RankingScores.post_id == pa_id)}
    check("ranking_scores: gleiche IDs, gleiche Werte", scores_neu == pa_scores_vorher)
    check("vote_count = 2", pa_neu.vote_count == 2, pa_neu.vote_count)
    check("Archivzeile weg", not archiv_da(remove_id))

    erwarte_fehler("Zweiter Restore derselben Aktion -> 404", 404, lambda: restore(remove_id))
    db.rollback()

    # ------------------------------------------------------------------
    print("\n2) Post zurueck, waehrenddessen Voter + Ort geloescht")
    pb = models.Post(title="Post B", content="Inhalt B", owner_id=a.id, location_id=loc.id, vote_count=2)
    db.add(pb)
    db.flush()
    pb_id = pb.id
    b_comment = models.Comments(user_id=b.id, post_id=pb_id, comment="von b")
    db.add_all([b_comment,
                models.Comments(user_id=tmp.id, post_id=pb_id, comment="von tmp"),
                models.Votes(user_id=b.id, post_id=pb_id),
                models.Votes(user_id=tmp.id, post_id=pb_id),
                models.RankingScores(voter_id=tmp.id, post_id=pb_id, direction=False, points=1)])
    db.flush()
    b_comment_id = b_comment.id

    admin.remove_post(pb_id, GRUND, db=db, current_user=mod)
    remove_id = letzte_action_id("delete_post")
    db.delete(tmp)  # tmp loescht seinen Account
    db.delete(loc)  # Ort fliegt aus dem Katalog
    db.commit()
    db.expire_all()

    out = restore(remove_id)
    check("extra meldet location_dropped",
          out.extra == {"restored_action_id": remove_id, "location_dropped": True}, out.extra)
    pb_neu = db.get(models.Post, pb_id)
    check("Post da, ohne Ort", pb_neu is not None and pb_neu.location_id is None)
    check("nur b's Kommentar zurueck", [c.id for c in db.query(models.Comments)
                                        .filter(models.Comments.post_id == pb_id)] == [b_comment_id])
    check("nur b's Vote zurueck", [v.user_id for v in db.query(models.Votes)
                                   .filter(models.Votes.post_id == pb_id)] == [b.id])
    check("tmp's ranking_score nicht zurueck",
          db.query(models.RankingScores).filter(models.RankingScores.post_id == pb_id).count() == 0)
    check("vote_count neu gezaehlt: 1 statt 2", pb_neu.vote_count == 1, pb_neu.vote_count)

    # ------------------------------------------------------------------
    print("\n3) Kommentar")
    k = models.Comments(user_id=b.id, post_id=pa_id, comment="wird zurueckgeholt")
    db.add(k)
    db.flush()
    k_id = k.id
    admin.remove_comment(k_id, GRUND, db=db, current_user=mod)
    out = restore(letzte_action_id("delete_comment"))
    k_neu = db.get(models.Comments, k_id)
    check("Kommentar zurueck, gleiche ID + Text", k_neu is not None and k_neu.comment == "wird zurueckgeholt"
          and k_neu.post_id == pa_id)

    pc = models.Post(title="Post C", content="x", owner_id=a.id)
    db.add(pc)
    db.flush()
    k2 = models.Comments(user_id=b.id, post_id=pc.id, comment="Post verschwindet")
    db.add(k2)
    db.flush()
    admin.remove_comment(k2.id, GRUND, db=db, current_user=mod)
    remove_id = letzte_action_id("delete_comment")
    db.delete(db.get(models.Post, pc.id))
    db.commit()
    log_vorher = db.query(M).filter(M.action == "restore_content").count()
    erwarte_fehler("Post weg -> 409", 409, lambda: restore(remove_id))
    db.rollback()
    check("  ... Archivzeile bleibt, kein Log", archiv_da(remove_id)
          and db.query(M).filter(M.action == "restore_content").count() == log_vorher)

    # ------------------------------------------------------------------
    print("\n4) Story")
    s = models.Story(owner_id=a.id, image_url=FAKE_URL)
    db.add(s)
    db.flush()
    s_id = s.id
    admin.remove_story(s_id, GRUND, db=db, current_user=mod)
    restore(letzte_action_id("delete_story"))
    s_neu = db.get(models.Story, s_id)
    check("frische Story zurueck, gleiche ID + Bild", s_neu is not None and s_neu.image_url == FAKE_URL)

    alt = models.Story(owner_id=a.id, image_url=FAKE_URL,
                       created_at=datetime.now(timezone.utc) - timedelta(days=2))
    db.add(alt)
    db.flush()
    admin.remove_story(alt.id, GRUND, db=db, current_user=mod)
    remove_id = letzte_action_id("delete_story")
    erwarte_fehler("2 Tage alte Story -> 409", 409, lambda: restore(remove_id))
    db.rollback()
    check("  ... Archivzeile bleibt", archiv_da(remove_id))

    # ------------------------------------------------------------------
    print("\n5) Profilbild")
    a.profile_picture_url = FAKE_URL
    db.flush()
    admin.remove_profile_picture(a.id, GRUND, db=db, current_user=mod)
    remove_id = letzte_action_id("delete_profile_picture")
    db.expire_all()

    db.get(models.User, a.id).profile_picture_url = "https://example.invalid/neues-bild.jpg"
    erwarte_fehler("User hat neues Bild -> 409", 409, lambda: restore(remove_id))
    db.rollback()  # neues Bild war nie committet -> wieder None
    check("  ... neues Bild nicht ueberschrieben (409 kam vor jeder Aenderung)", archiv_da(remove_id))

    restore(remove_id)
    check("ohne neues Bild: altes zurueck", db.get(models.User, a.id).profile_picture_url == FAKE_URL)

    # ------------------------------------------------------------------
    print("\n6) Rechte")
    p = inspect.signature(admin.restore_content).parameters["current_user"].default
    check("Route haengt an require_admin", p.dependency is admin.require_admin)
    erwarte_fehler("Mod kommt durch require_admin nicht durch -> 403", 403,
                   lambda: admin.require_admin(current_user=mod))
    erwarte_fehler("Unbekannte Aktion -> 404", 404, lambda: restore(999999999))
    db.rollback()

    print("\n7) Gesamtbild")
    check("S3 nie angefasst", s3_aufrufe == [], s3_aufrufe)

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
