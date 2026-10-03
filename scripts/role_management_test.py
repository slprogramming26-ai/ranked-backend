"""Prueft Phase 6 (PATCH /admin/users/{id}/role, GET /admin/users/{id}) gegen die
ECHTE DB — komplett in einer Transaktion, die am Ende zurueckgerollt wird
(gleicher Trick wie report_queue_test.py).

Start:  .\\venv\\Scripts\\python.exe scripts\\role_management_test.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models, schemas, oauth2  # noqa: E402
from app.database import engine  # noqa: E402
from app.routers.admin import change_role, get_user_detail, require_admin  # noqa: E402

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
    users = db.query(models.User).filter(models.User.role == "user").order_by(models.User.id).limit(5).all()
    if len(users) < 5:
        sys.exit("Brauche mindestens 5 User mit Rolle 'user'")
    a, b, mod, adm, adm2 = users
    mod.role, adm.role, adm2.role = "moderator", "admin", "admin"
    db.flush()

    M = models.ModerationAction

    print("\n1) Befoerdern / Herabstufen")
    out = change_role(a.id, schemas.RoleUpdate(role="moderator", reason="hilft viel"), db=db, current_user=adm)
    check("user -> moderator", out == {"user_id": a.id, "role": "moderator"}, out)
    log = db.query(M).filter(M.action == "role_change", M.target_user_id == a.id).order_by(M.id.desc()).first()
    check("Log: alte/neue Rolle + Grund", log is not None and log.moderator_id == adm.id
          and log.extra == {"old_role": "user", "new_role": "moderator"} and log.reason == "hilft viel",
          log and log.extra)
    out = change_role(a.id, schemas.RoleUpdate(role="user"), db=db, current_user=adm)
    check("moderator -> user (ohne Grund)", out["role"] == "user")
    db.expire_all()
    check("Rolle in der DB", db.get(models.User, a.id).role == "user")

    print("\n2) Verbotenes")
    erwarte_fehler("Mod darf keine Rollen aendern -> 403", 403, lambda: require_admin(current_user=mod))
    erwarte_fehler("Admin degradiert sich selbst -> 400", 400,
                   lambda: change_role(adm.id, schemas.RoleUpdate(role="user"), db=db, current_user=adm))
    erwarte_fehler("Admin degradiert anderen Admin -> 403", 403,
                   lambda: change_role(adm2.id, schemas.RoleUpdate(role="user"), db=db, current_user=adm))
    erwarte_fehler("Gleiche Rolle -> 400", 400,
                   lambda: change_role(b.id, schemas.RoleUpdate(role="user"), db=db, current_user=adm))
    erwarte_fehler("Unbekannter User -> 404", 404,
                   lambda: change_role(999999999, schemas.RoleUpdate(role="moderator"), db=db, current_user=adm))
    b.banned_until = datetime.now(timezone.utc) + timedelta(days=3)
    db.flush()
    erwarte_fehler("Gesperrten User befoerdern -> 400", 400,
                   lambda: change_role(b.id, schemas.RoleUpdate(role="moderator"), db=db, current_user=adm))
    try:
        schemas.RoleUpdate(role="superadmin")
        check("Unbekannte Rolle abgelehnt", False)
    except ValidationError:
        check("Unbekannte Rolle abgelehnt", True)

    print("\n3) User-Detail")
    db.add_all([
        models.Report(reporter_id=a.id, reported_user_id=b.id, reason="spam"),
        models.Report(reporter_id=mod.id, reported_user_id=b.id, reason="harassment"),
        models.Report(reporter_id=adm.id, reported_user_id=b.id, reason="other", status="dismissed"),
    ])
    db.add(M(moderator_id=mod.id, target_user_id=b.id, action="ban", reason="Testbann", extra={"days": 3}))
    db.flush()
    vorher_pending = db.query(models.Report).filter(models.Report.reported_user_id == b.id,
                                                    models.Report.status == "pending").count()
    vorher_total = db.query(models.Report).filter(models.Report.reported_user_id == b.id).count()

    detail = get_user_detail(b.id, db=db, current_user=mod)
    check("Meldungen offen", detail["reports_pending"] == vorher_pending, detail["reports_pending"])
    check("Meldungen gesamt", detail["reports_total"] == vorher_total, detail["reports_total"])
    check("Bann sichtbar", detail["banned_until"] is not None)
    neueste = detail["recent_actions"][0]
    check("Neueste Aktion = Testbann", neueste.action == "ban" and neueste.reason == "Testbann")
    check("Moderator per joinedload", neueste.moderator.username == mod.username)
    check("Schema serialisiert", schemas.AdminUserDetail.model_validate(detail).id == b.id)
    erwarte_fehler("Mod schaut Admin an -> 403", 403, lambda: get_user_detail(adm.id, db=db, current_user=mod))
    erwarte_fehler("Mod schaut sich selbst an -> 400", 400, lambda: get_user_detail(mod.id, db=db, current_user=mod))

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
