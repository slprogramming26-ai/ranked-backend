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
                             reason=reason, details=details, post_id=post.id if post else None))

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
    r = db.query(models.Report).filter(models.Report.post_id == p1.id).first()
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
            db.add(models.Report(reporter_id=b.id, reported_user_id=a.id, reason="Spam"))
            db.flush()
        check("alter Freitext 'Spam' abgelehnt", False)
    except IntegrityError:
        check("alter Freitext 'Spam' abgelehnt", True)

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
