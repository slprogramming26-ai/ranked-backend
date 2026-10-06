"""Prueft die Meldungs-Warteschlange (GET /admin/reports, POST /admin/reports/resolve)
gegen die ECHTE DB — aber komplett in einer Transaktion, die am Ende
zurueckgerollt wird. Es bleibt nichts in der DB stehen.

Trick: die Session haengt an einer Verbindung mit offener Aussen-Transaktion.
join_transaction_mode="create_savepoint" macht aus jedem db.commit() der Routen
nur einen SAVEPOINT; der Rollback am Ende verwirft alles.

Start:  .\\venv\\Scripts\\python.exe scripts\\report_queue_test.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models, schemas, oauth2  # noqa: E402
from app.database import engine  # noqa: E402
from app.routers.admin import list_reports, resolve_reports, list_actions  # noqa: E402
from app.routers.report import report_post, report_comment, report_story, report_user  # noqa: E402
from app.routers.comment import get_comments  # noqa: E402
from app.routers.story import get_stories  # noqa: E402
from app.routers.cleanup import clear_moderation_snapshots, SNAPSHOT_RETENTION_DAYS  # noqa: E402
from app.config import settings  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

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
    users = db.query(models.User).filter(models.User.role == "user").order_by(models.User.id).limit(4).all()
    if len(users) < 4:
        sys.exit("Brauche mindestens 4 User mit Rolle 'user'")
    a, b, c, d = users

    # Testbuehne: zwei Posts von a, ein "Moderator" m, ein "Admin" adm (nur in der Transaktion!)
    p1 = models.Post(title="Testpost 1", content="Inhalt 1", owner_id=a.id)
    p2 = models.Post(title="Testpost 2", content="Inhalt 2", owner_id=a.id)
    db.add_all([p1, p2])
    db.flush()
    mod, adm = c, d
    mod.role, adm.role = "moderator", "admin"

    def rep(reporter, reason, details=None, post=None, user=None):
        db.add(models.Report(reporter_id=reporter.id, reported_user_id=(post.owner_id if post else user.id),
                             reason=reason, details=details,
                             target_type="post" if post else "user", target_id=post.id if post else None))

    # p1: 2x spam + 1x inappropriate  -> 3 Meldungen
    rep(b, "spam", post=p1)
    rep(c, "spam", "Werbung fuer Kryptos", post=p1)
    rep(d, "inappropriate", post=p1)
    # p2: 1x misinformation
    rep(b, "misinformation", post=p2)
    # User a selbst: 1x harassment
    rep(b, "harassment", "beleidigt in Kommentaren", user=a)
    # Meldung GEGEN den Moderator -> darf nur der Admin sehen
    rep(b, "spam", user=mod)
    db.flush()

    print("\n1) Liste als Moderator")
    items = list_reports(report_status="pending", limit=20, skip=0, db=db, current_user=mod)
    eigene = [i for i in items if i["reported_user"].id in (a.id, mod.id)]
    ziele = [(i["target_type"], i["target_id"]) for i in eigene]
    check("p1 steht vorne (3 Meldungen)", ziele[:1] == [("post", p1.id)], ziele)
    check("p1 Aufteilung", eigene[0]["reasons"] == {"spam": 2, "inappropriate": 1}, eigene[0]["reasons"])
    check("p1 details ohne NULL", eigene[0]["details"] == ["Werbung fuer Kryptos"], eigene[0]["details"])
    check("p1 Vorschau", eigene[0]["preview"]["title"] == "Testpost 1")
    check("User-Report a dabei", ("user", a.id) in ziele)
    check("Meldung gegen Mod NICHT sichtbar", ("user", mod.id) not in ziele)

    print("\n2) Liste als Admin")
    items = list_reports(report_status="pending", limit=20, skip=0, db=db, current_user=adm)
    check("Admin sieht Meldung gegen Mod", ("user", mod.id) in [(i["target_type"], i["target_id"]) for i in items])

    print("\n3) Pagination")
    seite = list_reports(report_status="pending", limit=1, skip=0, db=db, current_user=adm)
    check("limit=1 -> 1 Eintrag", len(seite) == 1)

    print("\n4) Erledigen")
    out = resolve_reports(schemas.ReportResolve(target_type="post", target_id=p1.id, status="dismissed"),
                          db=db, current_user=mod)
    check("3 Meldungen auf einmal erledigt", out["resolved"] == 3, out)
    items = list_reports(report_status="pending", limit=20, skip=0, db=db, current_user=mod)
    check("p1 nicht mehr offen", ("post", p1.id) not in [(i["target_type"], i["target_id"]) for i in items])
    items = list_reports(report_status="dismissed", limit=20, skip=0, db=db, current_user=mod)
    check("p1 unter dismissed", ("post", p1.id) in [(i["target_type"], i["target_id"]) for i in items])
    r = db.query(models.Report).filter(models.Report.target_type == "post",
                                       models.Report.target_id == p1.id).first()
    check("resolved_by/at gesetzt", r.resolved_by == mod.id and r.resolved_at is not None)
    log = db.query(models.ModerationAction).filter(models.ModerationAction.moderator_id == mod.id,
                                                   models.ModerationAction.action == "resolve_reports").first()
    check("Audit-Zeile geschrieben", log is not None and log.target_id == p1.id
          and log.extra == {"target_type": "post", "status": "dismissed", "reports": 3},
          log and log.extra)

    print("\n4b) Audit-Log lesen (GET /admin/actions)")
    eintraege = list_actions(action=None, moderator_id=mod.id, target_user_id=None,
                             limit=50, skip=0, db=db, current_user=adm)
    check("Eintrag per moderator_id gefunden", any(e.id == log.id for e in eintraege))
    e = next(e for e in eintraege if e.id == log.id)
    check("Namen per joinedload dabei", e.moderator.username == mod.username
          and e.target_user.username == a.username)
    check("Schema serialisiert", schemas.ModerationActionOut.model_validate(e).action == "resolve_reports")
    check("Filter action=ban -> nicht dabei", all(x.id != log.id for x in list_actions(
        action="ban", moderator_id=mod.id, target_user_id=None, limit=50, skip=0, db=db, current_user=adm)))
    try:
        oauth2.require_role("admin")(current_user=mod)
        check("Mod darf Log NICHT lesen", False)
    except HTTPException as ex:
        check("Mod darf Log NICHT lesen", ex.status_code == 403)

    print("\n5) Verbotenes")
    try:
        resolve_reports(schemas.ReportResolve(target_type="user", target_id=mod.id, status="dismissed"),
                        db=db, current_user=mod)
        check("Mod kann Meldung gegen sich NICHT erledigen", False)
    except HTTPException as e:
        check("Mod kann Meldung gegen sich NICHT erledigen", e.status_code == 400, e.detail)
    try:
        resolve_reports(schemas.ReportResolve(target_type="post", target_id=p1.id, status="dismissed"),
                        db=db, current_user=mod)
        check("Zweites Erledigen -> 404", False)
    except HTTPException as e:
        check("Zweites Erledigen -> 404", e.status_code == 404)

    print("\n6) CHECK-Constraint")
    try:
        with db.begin_nested():
            db.add(models.Report(reporter_id=b.id, reported_user_id=a.id, reason="Spam", target_type="user"))
            db.flush()
        check("alter Freitext 'Spam' abgelehnt", False)
    except IntegrityError:
        check("alter Freitext 'Spam' abgelehnt", True)
    for name, ttype, tid in [("('post', NULL) abgelehnt", "post", None),
                             ("('user', 5) abgelehnt", "user", 5),
                             ("unbekannter target_type abgelehnt", "group", 1)]:
        try:
            with db.begin_nested():
                db.add(models.Report(reporter_id=b.id, reported_user_id=a.id, reason="spam",
                                     target_type=ttype, target_id=tid))
                db.flush()
            check(name, False)
        except IntegrityError:
            check(name, True)

    print("\n7) Melden ueber /report (report.py)")
    p3 = models.Post(title="Neuer Titel", content="Neuer Inhalt", owner_id=a.id)
    st = models.Story(owner_id=a.id, image_url="https://example.invalid/x.jpg")
    db.add_all([p3, st])
    db.flush()
    cm = models.Comments(user_id=a.id, post_id=p3.id, comment="Fieser Kommentar")
    db.add(cm)
    db.flush()
    grund = schemas.ReportCreate(reason="spam")
    report_post(p3.id, grund, db=db, current_user=b)
    report_comment(cm.id, grund, db=db, current_user=b)
    report_story(st.id, grund, db=db, current_user=b)
    report_user(a.id, grund, db=db, current_user=c)
    R = models.Report

    def gemeldet(ttype, tid, reporter):
        return db.query(R).filter(R.reporter_id == reporter.id, R.target_type == ttype,
                                  R.target_id == tid if tid is not None else R.target_id.is_(None)).first()

    r = gemeldet("post", p3.id, b)
    check("Post: Snapshot Titel + Text", r.content_snapshot == "Neuer Titel\n\nNeuer Inhalt", repr(r.content_snapshot))
    check("Kommentar: Snapshot = Text", gemeldet("comment", cm.id, b).content_snapshot == "Fieser Kommentar")
    check("Story: kein Snapshot", gemeldet("story", st.id, b).content_snapshot is None)
    r = gemeldet("user", None, c)
    check("User: target_id NULL", r is not None and r.reported_user_id == a.id and r.content_snapshot is None)
    for name, fn in [("Doppelt Post -> 409", lambda: report_post(p3.id, grund, db=db, current_user=b)),
                     ("Doppelt User -> 409", lambda: report_user(a.id, grund, db=db, current_user=c)),
                     ("Selbst melden -> 400", lambda: report_post(p3.id, grund, db=db, current_user=a))]:
        try:
            fn()
            check(name, False)
        except HTTPException as e:
            check(name, e.status_code == (400 if "Selbst" in name else 409), e.detail)
    # Gleicher Melder, ANDERER User -> kein Duplikat (target_id ist bei beiden NULL!)
    report_user(b.id, grund, db=db, current_user=c)
    check("User-Meldung gegen anderen User -> kein 409",
          db.query(R).filter(R.reporter_id == c.id, R.target_type == "user").count() == 2)

    print("\n8) Ich-habe-gemeldet-Filter (Kommentare + Storys)")
    # merge statt add: in der echten DB folgt b dem a evtl. schon.
    db.merge(models.Follows(follower_id=b.id, followee_id=a.id))
    db.flush()
    ids = [k["id"] for k in get_comments(p3.id, db=db, current_user=b)]
    check("b sieht gemeldeten Kommentar NICHT", cm.id not in ids)
    ids = [k["id"] for k in get_comments(p3.id, db=db, current_user=d)]
    check("d sieht ihn weiterhin", cm.id in ids)
    ids = [s["id"] for s in get_stories(db=db, current_user=b)]
    check("b sieht gemeldete Story NICHT", st.id not in ids)
    eigene = [s for s in get_stories(db=db, current_user=a) if s["id"] == st.id]
    check("a sieht eigene Story (mit owner)", len(eigene) == 1 and eigene[0]["owner"].id == a.id)

    print("\n9) Inhalt vom User selbst geloescht -> Meldung bleibt")
    cm_id, st_id, p3_id = cm.id, st.id, p3.id
    p3.title, p3.content = "Harmlos bearbeitet", "nichts zu sehen"  # nach dem Melden geaendert
    db.delete(cm)
    db.delete(st)
    db.flush()
    items = list_reports(report_status="pending", limit=100, skip=0, db=db, current_user=mod)
    nach_ziel = {(i["target_type"], i["target_id"]): i for i in items}
    k = nach_ziel.get(("comment", cm_id))
    check("Kommentar-Meldung noch in der Queue", k is not None)
    check("  content_deleted + Snapshot als Vorschau", k and k["content_deleted"]
          and k["preview"] == {"content": "Fieser Kommentar"}, k and k["preview"])
    s = nach_ziel.get(("story", st_id))
    check("Story-Meldung noch da, ohne Vorschau", s and s["content_deleted"] and s["preview"] is None)
    p = nach_ziel.get(("post", p3_id))
    check("Post lebt -> live-Vorschau (bearbeitet), nicht geloescht",
          p and not p["content_deleted"] and p["preview"]["title"] == "Harmlos bearbeitet")
    check("Schema serialisiert", schemas.ReportQueueItem.model_validate(k).content_deleted is True)

    print("\n10) Cleanup leert nur alte, ERLEDIGTE Snapshots")
    r_alt = gemeldet("post", p3_id, b)
    r_offen = db.query(R).filter(R.target_type == "comment", R.target_id == cm_id).first()
    r_alt.status, r_alt.resolved_at = "dismissed", datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_RETENTION_DAYS + 1)
    r_offen.created_at = datetime.now(timezone.utc) - timedelta(days=SNAPSHOT_RETENTION_DAYS + 1)
    db.flush()
    out = clear_moderation_snapshots(db=db, x_cleanup_secret=settings.story_cleanup_secret)
    db.expire_all()
    check("Antwort hat cleared_reports", out["cleared_reports"] >= 1, out)
    check("alte erledigte Meldung: Snapshot leer", gemeldet("post", p3_id, b).content_snapshot is None)
    check("alte OFFENE Meldung: Snapshot bleibt",
          db.query(R).filter(R.target_type == "comment", R.target_id == cm_id).first().content_snapshot
          == "Fieser Kommentar")

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
