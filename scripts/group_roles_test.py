"""Prueft Gruender + Admins in Gruppen: create (mit/ohne Namen), members mit role,
PATCH .../role, kick, PATCH Name, transfer, leave, delete und die automatische
Uebergabe bei Account-Loeschung — gegen die ECHTE DB, komplett in einer Transaktion,
die am Ende zurueckgerollt wird (gleicher Trick wie banned_account_delete_test.py).
Das commit() in den Routen gibt dabei nur einen Savepoint frei.

Achtung: In EINER Transaktion liefert now() immer dieselbe Zeit -> joined_at wird
bei der Account-Loeschung von Hand gesetzt, sonst gibt es kein "aeltestes Mitglied".

Start:  .\\venv\\Scripts\\python.exe scripts\\group_roles_test.py
"""
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models, schemas, utils  # noqa: E402
from app.database import engine  # noqa: E402
from app.routers import group_chat as gc  # noqa: E402
from app.routers import user as user_router  # noqa: E402

# S3 abklemmen (rollt kein Rollback zurueck) und mitschreiben, was geloescht wuerde.
s3_geloescht = []
user_router.delete_s3_object = lambda url, db: s3_geloescht.append(url)
user_router.delete_post_image = lambda url, db: None
user_router.delete_story_image = lambda url, db: None
gc.delete_s3_object = lambda url, db: s3_geloescht.append(url)

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

PW_HASH = utils.hash_password("test-pw-12345")
tag = secrets.token_hex(4)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def neuer_user(name):
    u = models.User(email=f"grp_{name}_{tag}@example.com", username=f"grp_{name}_{tag}",
                    passwort=PW_HASH, email_verified=True)
    db.add(u)
    db.flush()
    return u


def beitreten(group_chat_id, user, role="member", tage=0):
    # tage: Abstand zu T0 -> bestimmt, wer "aelter" in der Gruppe ist
    db.add(models.GroupChatMembership(group_chat_id=group_chat_id, participant_id=user.id,
                                      role=role, joined_at=T0 + timedelta(days=tage)))
    db.flush()


def gruppe(group_chat_id):
    db.expire_all()  # Routen schreiben teils per Bulk-Update -> frisch lesen
    return db.query(models.GroupChats).filter(models.GroupChats.group_chat_id == group_chat_id).first()


def rolle_in_db(group_chat_id, user):
    m = gc._get_membership(db, group_chat_id, user.id)
    return m.role if m else None


def rekey_zuruecksetzen(group_chat_id):
    gruppe(group_chat_id).needs_rekey = False
    db.flush()


def members_map(group_chat_id, als):
    rows = gc.get_group_members(group_chat_id=group_chat_id, db=db, current_user=als)
    return {r["user_id"]: r["role"] for r in rows}


def role(gid, uid, neue_rolle, als):
    return gc.change_member_role(group_chat_id=gid, user_id=uid,
                                 role_update=schemas.GroupMemberRoleUpdate(role=neue_rolle),
                                 db=db, current_user=als)


def kick(gid, uid, als):
    return gc.kick_from_group(group_chat_id=gid, user_id=uid, db=db, current_user=als)


try:
    gruender = neuer_user("gruender")
    adm = neuer_user("adm")
    mit = neuer_user("mit")
    mit2 = neuer_user("mit2")
    fremd = neuer_user("fremd")

    print("\n1) create mit und ohne Namen")
    resp = gc.create_group_chat(group_create=None, db=db, current_user=gruender)
    g_ohne = gruppe(resp["group_chat_id"])
    check("ohne Body -> Gruppe ohne Namen", g_ohne and g_ohne.group_name is None)
    check("Gruender ist Mitglied", rolle_in_db(g_ohne.group_chat_id, gruender) == "member",
          "(role-Zeile des Gruenders bleibt 'member')")

    resp = gc.create_group_chat(group_create=schemas.GroupChatCreate(group_name="Testgruppe"),
                                db=db, current_user=gruender)
    gid = resp["group_chat_id"]
    check("mit Namen -> group_name gesetzt", gruppe(gid).group_name == "Testgruppe")
    check("creator_id = Ersteller", gruppe(gid).creator_id == gruender.id)

    beitreten(gid, adm, tage=1)
    beitreten(gid, mit, tage=2)
    beitreten(gid, mit2, tage=3)

    print("\n2) members liefert role")
    m = members_map(gid, gruender)
    check("Gruender = creator", m.get(gruender.id) == "creator", m.get(gruender.id))
    check("andere = member", m.get(adm.id) == m.get(mit.id) == "member")
    erwarte_fehler("Nicht-Mitglied sieht members nicht -> 403", 403,
                   lambda: members_map(gid, fremd))

    print("\n3) Rollen aendern")
    try:
        schemas.GroupMemberRoleUpdate(role="creator")
        check("role='creator' im Body abgelehnt", False, "kein ValidationError")
    except ValidationError:
        check("role='creator' im Body abgelehnt", True, "(ValidationError -> 422)")

    resp = role(gid, adm.id, "admin", gruender)
    check("Gruender befoerdert -> 200", resp == {"user_id": adm.id, "role": "admin"}, resp)
    check("members zeigt admin", members_map(gid, mit)[adm.id] == "admin")

    erwarte_fehler("Mitglied befoerdert -> 403", 403, lambda: role(gid, mit2.id, "admin", mit))
    erwarte_fehler("Nicht-Mitglied befoerdert -> 403", 403, lambda: role(gid, mit.id, "admin", fremd))
    erwarte_fehler("Gruender-Rolle aendern -> 403", 403, lambda: role(gid, gruender.id, "member", adm))
    erwarte_fehler("Ziel nicht in Gruppe -> 404", 404, lambda: role(gid, fremd.id, "admin", gruender))
    erwarte_fehler("unbekannte Gruppe -> 404", 404, lambda: role(-1, mit.id, "admin", gruender))

    resp = role(gid, mit.id, "admin", adm)
    check("Admin befoerdert Mitglied -> 200", resp["role"] == "admin" and rolle_in_db(gid, mit) == "admin")
    erwarte_fehler("Admin degradiert Admin -> 403", 403, lambda: role(gid, mit.id, "member", adm))
    erwarte_fehler("Admin degradiert sich selbst -> 403", 403, lambda: role(gid, adm.id, "member", adm))
    resp = role(gid, mit.id, "member", gruender)
    check("Gruender degradiert Admin -> 200", rolle_in_db(gid, mit) == "member", resp)
    resp = role(gid, mit.id, "member", gruender)
    check("gleiche Rolle nochmal -> trotzdem 200", resp == {"user_id": mit.id, "role": "member"})

    print("\n4) kick")
    erwarte_fehler("Mitglied kickt -> 403", 403, lambda: kick(gid, mit2.id, mit))
    erwarte_fehler("Admin kickt Gruender -> 403", 403, lambda: kick(gid, gruender.id, adm))
    erwarte_fehler("Gruender kickt sich selbst -> 403", 403, lambda: kick(gid, gruender.id, gruender))
    erwarte_fehler("Admin kickt sich selbst -> 403", 403, lambda: kick(gid, adm.id, adm))
    erwarte_fehler("kick Nicht-Mitglied -> 404", 404, lambda: kick(gid, fremd.id, gruender))

    role(gid, mit.id, "admin", gruender)
    erwarte_fehler("Admin kickt Admin -> 403", 403, lambda: kick(gid, mit.id, adm))
    role(gid, mit.id, "member", gruender)

    rekey_zuruecksetzen(gid)
    kick(gid, mit2.id, adm)
    check("Admin kickt Mitglied -> raus", rolle_in_db(gid, mit2) is None)
    check("needs_rekey gesetzt", gruppe(gid).needs_rekey is True)

    beitreten(gid, mit2, tage=4)
    role(gid, mit2.id, "admin", gruender)
    kick(gid, mit2.id, gruender)
    check("Gruender kickt Admin -> raus", rolle_in_db(gid, mit2) is None)

    print("\n5) PATCH Name")
    resp = gc.update_group_chat(group_chat_id=gid, group_update=schemas.GroupChatUpdate(group_name="Neu"),
                                db=db, current_user=adm)
    check("Admin aendert Namen -> {message, group}",
          resp["message"] == "Group updated successfully" and resp["group"].group_name == "Neu", resp)
    erwarte_fehler("Mitglied aendert Namen -> 403", 403,
                   lambda: gc.update_group_chat(group_chat_id=gid,
                                                group_update=schemas.GroupChatUpdate(group_name="x"),
                                                db=db, current_user=mit))
    erwarte_fehler("leerer Body -> 400", 400,
                   lambda: gc.update_group_chat(group_chat_id=gid, group_update=schemas.GroupChatUpdate(),
                                                db=db, current_user=gruender))

    print("\n6) transfer")

    def transfer(uid, als):
        return gc.transfer_group(group_chat_id=gid, user_id=uid, db=db, current_user=als)

    erwarte_fehler("Admin uebergibt -> 403", 403, lambda: transfer(mit.id, adm))
    erwarte_fehler("Mitglied uebergibt -> 403", 403, lambda: transfer(mit.id, mit))
    erwarte_fehler("an sich selbst -> 400", 400, lambda: transfer(gruender.id, gruender))
    erwarte_fehler("an Nicht-Mitglied -> 404", 404, lambda: transfer(fremd.id, gruender))

    resp = transfer(mit.id, gruender)
    check("Uebergabe an Mitglied -> 200", resp == {"group_chat_id": gid, "creator_id": mit.id}, resp)
    m = members_map(gid, mit)
    check("neuer Gruender = creator", m[mit.id] == "creator", m[mit.id])
    check("alter Gruender = admin", m[gruender.id] == "admin", m[gruender.id])
    check("Admin bleibt admin", m[adm.id] == "admin")
    check("role-Zeile des neuen Gruenders = admin", rolle_in_db(gid, mit) == "admin")
    erwarte_fehler("alter Gruender uebergibt nochmal -> 403", 403, lambda: transfer(adm.id, gruender))

    print("\n7) leave")
    leave = lambda als: gc.leave_group_chat(group_chat_id=gid, db=db, current_user=als)  # noqa: E731
    erwarte_fehler("neuer Gruender verlaesst -> 403", 403, lambda: leave(mit))
    erwarte_fehler("Nicht-Mitglied verlaesst -> 404", 404, lambda: leave(fremd))
    rekey_zuruecksetzen(gid)
    resp = leave(gruender)
    check("alter Gruender (jetzt Admin) verlaesst -> 200", resp == {"message": "left"}, resp)
    check("ist raus", rolle_in_db(gid, gruender) is None)
    check("needs_rekey gesetzt", gruppe(gid).needs_rekey is True)

    print("\n8) Gruppe loeschen")
    delete = lambda als: gc.delete_group_chat(group_chat_id=gid, db=db, current_user=als)  # noqa: E731
    erwarte_fehler("Admin loescht -> 403", 403, lambda: delete(adm))
    gruppe(gid).profile_picture = "https://example.com/grp_bild_8.jpg"
    db.flush()
    s3_geloescht.clear()
    delete(mit)
    check("Gruender loescht -> Gruppe weg", gruppe(gid) is None)
    check("Bild an S3-Loeschung gegeben", s3_geloescht == ["https://example.com/grp_bild_8.jpg"], s3_geloescht)

    print("\n9) Account-Loeschung des Gruenders")
    weg = neuer_user("weg")
    a_alt = neuer_user("a_alt")      # aeltestes Mitglied, aber nur member
    a_admin = neuer_user("a_admin")  # juengerer Admin -> muss trotzdem gewinnen
    b_alt = neuer_user("b_alt")
    b_neu = neuer_user("b_neu")
    c_gruender = neuer_user("c_gruender")

    def gruppe_von(ersteller, bild=None):
        g = models.GroupChats(creator_id=ersteller.id, profile_picture=bild)
        db.add(g)
        db.flush()
        beitreten(g.group_chat_id, ersteller, tage=0)
        return g.group_chat_id

    g_admin = gruppe_von(weg)                # Nachfolger: aeltester Admin
    beitreten(g_admin, a_alt, tage=1)
    beitreten(g_admin, a_admin, role="admin", tage=5)

    g_mitglied = gruppe_von(weg)             # nur Mitglieder -> aeltestes
    beitreten(g_mitglied, b_neu, tage=7)
    beitreten(g_mitglied, b_alt, tage=2)

    g_allein = gruppe_von(weg, bild="https://example.com/grp_allein.jpg")  # niemand mehr -> stirbt

    g_fremd = gruppe_von(c_gruender)         # weg ist hier nur Mitglied
    beitreten(g_fremd, weg, tage=3)

    for g in (g_admin, g_mitglied, g_allein, g_fremd):
        rekey_zuruecksetzen(g)
    s3_geloescht.clear()

    weg_id = weg.id
    resp = user_router.delete_account(current_user=weg, db=db)
    check("DELETE /users/ -> 204", resp.status_code == 204, resp.status_code)
    check("User geloescht", db.query(models.User.id).filter(models.User.id == weg_id).first() is None)

    g = gruppe(g_admin)
    check("Gruppe mit Admin: juengerer Admin wird Gruender (vor aelterem Mitglied)",
          g and g.creator_id == a_admin.id, g and g.creator_id)
    check("  needs_rekey", g and g.needs_rekey is True)
    check("  aelteres Mitglied bleibt member", rolle_in_db(g_admin, a_alt) == "member")

    g = gruppe(g_mitglied)
    check("Gruppe nur Mitglieder: aeltestes wird Gruender", g and g.creator_id == b_alt.id, g and g.creator_id)
    check("  Nachfolger ist jetzt admin", rolle_in_db(g_mitglied, b_alt) == "admin")
    check("  anderes Mitglied bleibt member", rolle_in_db(g_mitglied, b_neu) == "member")
    check("  needs_rekey", g and g.needs_rekey is True)

    check("Gruppe allein: per CASCADE weg", gruppe(g_allein) is None)
    # None in der Liste = Profilbild des Users (hat keins), das ignoriert delete_s3_object
    check("  Bild an S3-Loeschung gegeben", "https://example.com/grp_allein.jpg" in s3_geloescht, s3_geloescht)

    g = gruppe(g_fremd)
    check("fremde Gruppe: Gruender unveraendert", g and g.creator_id == c_gruender.id)
    # weg.id nicht mehr lesen: das Objekt ist abgelaufen, die Zeile geloescht
    check("  Mitgliedschaft weg (CASCADE)", g and gc._get_membership(db, g_fremd, weg_id) is None)
    check("  needs_rekey", g and g.needs_rekey is True)

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
