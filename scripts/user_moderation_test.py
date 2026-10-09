"""Prueft Phase 5 removed_content: GET /users/moderation (eigene Massnahmen, DSA Art. 17) und die
Activities content_removed / content_restored. Laeuft gegen die ECHTE DB, komplett in einer
Transaktion, die am Ende zurueckgerollt wird (gleiches Muster wie content_restore_test.py).

S3: die drei delete_s3_object-Funktionen sind durch Spione ersetzt (vor dem admin-Import).

Start:  .\\venv\\Scripts\\python.exe scripts\\user_moderation_test.py
"""
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
    a, b, mod, adm = users
    mod.role, adm.role = "moderator", "admin"
    a.banned_until = a.ban_reason = None
    db.flush()

    M = models.ModerationAction
    A = models.Activity

    def letzte_action_id(action_name):
        return db.query(M.id).filter(M.target_user_id == a.id, M.action == action_name) \
                 .order_by(M.id.desc()).first()[0]

    def activities(user, typ):
        return [x.payload for x in db.query(A).filter(A.user_id == user.id, A.type == typ).order_by(A.id)]

    def liste(user, **kw):
        out = user_router.get_my_moderation(current_user=user, db=db, **{"limit": 50, "skip": 0, **kw})
        db.expire_all()
        return out

    # Was a VOR dem Test schon hatte (echte DB) - wird unten herausgerechnet
    alt_ids = {x["id"] for x in liste(a, limit=200)}
    removed_vorher = activities(a, "content_removed")

    # ------------------------------------------------------------------
    print("\n1) Vier Entfernungen -> je eine Activity content_removed mit der Log-ID")
    p = models.Post(title="Mod-Test", content="boeser Inhalt", owner_id=a.id, image_url=FAKE_URL)
    s = models.Story(owner_id=a.id, image_url=FAKE_URL)
    db.add_all([p, s])
    db.flush()
    k = models.Comments(user_id=a.id, post_id=p.id, comment="boeser Kommentar")
    db.add(k)
    db.flush()
    a.profile_picture_url = FAKE_URL
    db.flush()

    admin.remove_comment(k.id, schemas.ContentRemove(reason="harassment"), db=db, current_user=mod)
    id_comment = letzte_action_id("delete_comment")
    admin.remove_post(p.id, schemas.ContentRemove(reason="spam", details="Werbelink"), db=db, current_user=mod)
    id_post = letzte_action_id("delete_post")
    admin.remove_story(s.id, schemas.ContentRemove(reason="inappropriate"), db=db, current_user=mod)
    id_story = letzte_action_id("delete_story")
    admin.remove_profile_picture(a.id, schemas.ContentRemove(reason="inappropriate"), db=db, current_user=mod)
    id_pic = letzte_action_id("delete_profile_picture")
    db.expire_all()

    neu = activities(a, "content_removed")[len(removed_vorher):]
    check("4 Activities, payload = Log-IDs in Reihenfolge", neu == [id_comment, id_post, id_story, id_pic], neu)
    empfaenger = {x.user_id for x in db.query(A).filter(A.type == "content_removed", A.payload.in_(neu))}
    check("Empfaenger ist nur der Besitzer (nicht der Mod)", empfaenger == {a.id}, empfaenger)

    # ------------------------------------------------------------------
    print("\n2) Bann (7 Tage) + Aufheben, danach permanenter Bann von Admin")
    activities_vorher = db.query(A).filter(A.user_id == a.id).count()
    admin.ban_user(a.id, schemas.BanCreate(days=7, reason="Mehrfach Spam"), db=db, current_user=mod)
    id_ban7 = letzte_action_id("ban")
    admin.unban_user(a.id, db=db, current_user=mod)
    admin.ban_user(a.id, schemas.BanCreate(reason="Endgueltig"), db=db, current_user=adm)
    id_ban_perm = letzte_action_id("ban")
    admin.unban_user(a.id, db=db, current_user=adm)  # sonst gibt es Aerger, falls a echt ist
    db.expire_all()
    check("Bann/Unban erzeugen KEINE Activity",
          db.query(A).filter(A.user_id == a.id).count() == activities_vorher)

    # ------------------------------------------------------------------
    print("\n3) Restore der Story -> Activity content_restored mit der ALTEN Log-ID")
    admin.restore_content(id_story, db=db, current_user=adm)
    db.expire_all()
    check("content_restored payload = Loesch-Aktion", activities(a, "content_restored")[-1:] == [id_story],
          activities(a, "content_restored"))

    # ------------------------------------------------------------------
    print("\n4) GET /users/moderation fuer a")
    out = [x for x in liste(a, limit=200) if x["id"] not in alt_ids]
    ids = [x["id"] for x in out]
    erwartet = [id_ban_perm, id_ban7, id_pic, id_story, id_post, id_comment]
    check("genau 6 Eintraege, neueste zuerst (kein unban/restore_content)", ids == erwartet, (ids, erwartet))

    by_id = {x["id"]: x for x in out}
    check("Story: restored = True", by_id[id_story]["restored"] is True)
    check("alle anderen: restored = False",
          all(by_id[i]["restored"] is False for i in erwartet if i != id_story))
    check("Post: reason + details + Snapshot",
          by_id[id_post]["reason"] == "spam" and by_id[id_post]["details"] == "Werbelink"
          and by_id[id_post]["content_snapshot"] == "Mod-Test\n\nboeser Inhalt", by_id[id_post])
    check("Kommentar: Snapshot = Text, details None",
          by_id[id_comment]["content_snapshot"] == "boeser Kommentar" and by_id[id_comment]["details"] is None)
    check("Story: kein Snapshot", by_id[id_story]["content_snapshot"] is None)
    check("Bann 7 Tage: ban_days = 7, reason Freitext",
          by_id[id_ban7]["ban_days"] == 7 and by_id[id_ban7]["reason"] == "Mehrfach Spam")
    check("Permanenter Bann: ban_days = None", by_id[id_ban_perm]["ban_days"] is None)
    check("delete_*: ban_days immer None", all(by_id[i]["ban_days"] is None for i in erwartet[2:]))
    check("appeal_until = created_at + 180 Tage",
          all(x["appeal_until"] - x["created_at"] == timedelta(days=180) for x in out))

    validiert = [schemas.MyModerationActionOut.model_validate(x) for x in out]
    check("Alles passt ins Schema", len(validiert) == 6)
    felder = set(schemas.MyModerationActionOut.model_fields)
    check("Kein Moderator im Schema", not any("moderator" in f for f in felder), felder)

    # ------------------------------------------------------------------
    print("\n5) Blaettern + fremde Sicht")
    # Die Testeintraege sind die neuesten -> sie stehen vorn, auch wenn a echte aeltere hat
    check("limit=2 -> die 2 neuesten", [x["id"] for x in liste(a, limit=2)] == erwartet[:2])
    check("skip=2, limit=2 -> die naechsten 2", [x["id"] for x in liste(a, limit=2, skip=2)] == erwartet[2:4])
    check("b sieht keinen von a's Eintraegen", not set(ids) & {x["id"] for x in liste(b, limit=200)})

    # ------------------------------------------------------------------
    print("\n6) Routen-Reihenfolge")
    pfade = [r.path for r in user_router.router.routes]
    check("/users/moderation steht VOR /users/{id}",
          pfade.index("/users/moderation") < pfade.index("/users/{id}"), pfade)

    print("\n7) Gesamtbild")
    check("S3 nie angefasst", s3_aufrufe == [], s3_aufrufe)

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
