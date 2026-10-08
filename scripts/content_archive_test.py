"""Prueft _archive (Phase 2 removed_content): Mod-Entfernungen landen im Archiv, und zwar
GENAU der eine Inhalt (bei Posts mit seinen Kindern) — nichts fehlt, nichts Fremdes kommt mit.
Laeuft gegen die ECHTE DB, komplett in einer Transaktion, die am Ende zurueckgerollt wird
(gleiches Muster wie content_removal_test.py).

S3: die drei delete_s3_object-Funktionen (post/story/user) werden durch Spione ersetzt,
BEVOR admin importiert wird (sonst haette ein "from .post import ..." dort noch das
Original). Die Routen duerfen sie NICHT aufrufen (Bild bleibt bis zum Cleanup-Cron).

Start:  .\\venv\\Scripts\\python.exe scripts\\content_archive_test.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models, schemas  # noqa: E402
from app.database import engine  # noqa: E402
from app.routers import post as post_router, story as story_router, user as user_router  # noqa: E402

# --- S3-Spione: zaehlen jeden Aufruf statt zu loeschen ---
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

    RC = models.RemovedContent
    M = models.ModerationAction

    def archiv_anzahl():
        return db.query(RC).count()

    def archiv_zu(action_name):
        """Archivzeile zur letzten Log-Zeile dieser Aktion von mod."""
        log = db.query(M).filter(M.moderator_id == mod.id, M.action == action_name) \
                .order_by(M.id.desc()).first()
        return log, db.query(RC).filter(RC.moderation_action_id == log.id).one_or_none()

    # --- Testbuehne: post1 wird entfernt, post2 ist die Kontrollgruppe ---
    post1 = models.Post(title="Weg damit", content="Boese", owner_id=a.id, image_url=FAKE_URL, flag="creativity")
    post2 = models.Post(title="Bleibt", content="Harmlos", owner_id=a.id)
    story = models.Story(owner_id=a.id, image_url=FAKE_URL)
    db.add_all([post1, post2, story])
    db.flush()

    # post1: 3 Kommentare, 2 Votes, 2 ranking_scores
    p1_comments = [models.Comments(user_id=u.id, post_id=post1.id, comment=f"p1 von {u.id}") for u in (b, mod, b)]
    p1_votes = [models.Votes(user_id=u.id, post_id=post1.id) for u in (b, adm)]
    p1_scores = [models.RankingScores(voter_id=u.id, post_id=post1.id, direction=True, points=3) for u in (b, adm)]
    # post2: 2 Kommentare, 1 Vote, 1 ranking_score
    p2_comments = [models.Comments(user_id=u.id, post_id=post2.id, comment=f"p2 von {u.id}") for u in (b, adm)]
    p2_votes = [models.Votes(user_id=b.id, post_id=post2.id)]
    p2_scores = [models.RankingScores(voter_id=b.id, post_id=post2.id, direction=False, points=1)]
    db.add_all(p1_comments + p1_votes + p1_scores + p2_comments + p2_votes + p2_scores)
    a.profile_picture_url = FAKE_URL
    db.flush()

    post1_id, post2_id, story_id = post1.id, post2.id, story.id
    p1_comment_ids = {c.id for c in p1_comments}
    p1_score_ids = {r.id for r in p1_scores}
    p1_voter_ids = {v.user_id for v in p1_votes}
    p2_comment_ids = {c.id for c in p2_comments}
    p2_score_ids = {r.id for r in p2_scores}
    start_anzahl = archiv_anzahl()

    # ------------------------------------------------------------------
    print("\n1) Post mit Kindern entfernen")
    remove_post_req = schemas.ContentRemove(reason="spam")
    admin.remove_post(post1_id, remove_post_req, db=db, current_user=mod)
    db.expire_all()

    check("genau EINE neue Archivzeile", archiv_anzahl() == start_anzahl + 1, archiv_anzahl() - start_anzahl)
    log, row = archiv_zu("delete_post")
    check("Archivzeile haengt an der Log-Zeile", row is not None)
    check("Metadaten: type/target/owner/bild",
          row.target_type == "post" and row.target_id == post1_id and row.owner_id == a.id
          and row.image_url == FAKE_URL,
          (row.target_type, row.target_id, row.owner_id, row.image_url))
    d = row.data
    check("data hat genau die 4 Schluessel", set(d) == {"post", "comments", "votes", "ranking_scores"}, sorted(d))
    check("post: richtige Zeile, alle Spalten", d["post"]["id"] == post1_id and d["post"]["title"] == "Weg damit"
          and d["post"]["flag"] == "creativity"
          and set(d["post"]) == {c.name for c in models.Post.__table__.columns}, sorted(d["post"]))

    check("comments: exakt die 3 von post1", {c["id"] for c in d["comments"]} == p1_comment_ids
          and len(d["comments"]) == 3, [c["id"] for c in d["comments"]])
    check("votes: exakt die 2 von post1", {v["user_id"] for v in d["votes"]} == p1_voter_ids
          and len(d["votes"]) == 2, d["votes"])
    check("ranking_scores: exakt die 2 von post1", {r["id"] for r in d["ranking_scores"]} == p1_score_ids
          and len(d["ranking_scores"]) == 2, [r["id"] for r in d["ranking_scores"]])
    alle_kinder = d["comments"] + d["votes"] + d["ranking_scores"]
    check("KEIN Kind von einem fremden Post", all(k["post_id"] == post1_id for k in alle_kinder),
          {k["post_id"] for k in alle_kinder})
    check("post2-Kinder tauchen nirgends auf",
          not ({c["id"] for c in d["comments"]} & p2_comment_ids)
          and not ({r["id"] for r in d["ranking_scores"]} & p2_score_ids))

    check("Original: post1 weg", db.get(models.Post, post1_id) is None)
    check("Original: Kinder von post1 weg (CASCADE)",
          db.query(models.Comments).filter(models.Comments.post_id == post1_id).count() == 0
          and db.query(models.Votes).filter(models.Votes.post_id == post1_id).count() == 0
          and db.query(models.RankingScores).filter(models.RankingScores.post_id == post1_id).count() == 0)
    check("Kontrolle: post2 + Kinder unangetastet",
          db.get(models.Post, post2_id) is not None
          and {c.id for c in db.query(models.Comments).filter(models.Comments.post_id == post2_id)} == p2_comment_ids
          and db.query(models.Votes).filter(models.Votes.post_id == post2_id).count() == 1
          and {r.id for r in db.query(models.RankingScores).filter(models.RankingScores.post_id == post2_id)}
          == p2_score_ids)

    # ------------------------------------------------------------------
    print("\n2) Kommentar entfernen (auf post2)")
    ziel = p2_comments[0]
    ziel_id, ziel_user, ziel_text = ziel.id, ziel.user_id, ziel.comment
    vorher = archiv_anzahl()
    # Kommentar von b -> Rangregel gegen b, mod darf das
    admin.remove_comment(ziel_id, schemas.ContentRemove(reason="harassment"), db=db, current_user=mod)
    db.expire_all()

    check("genau EINE neue Archivzeile", archiv_anzahl() == vorher + 1)
    log, row = archiv_zu("delete_comment")
    check("Metadaten", row is not None and row.target_type == "comment" and row.target_id == ziel_id
          and row.owner_id == ziel_user and row.image_url is None)
    check("data = genau diese eine Kommentarzeile",
          row.data == {"id": ziel_id, "user_id": ziel_user, "post_id": post2_id, "comment": ziel_text}, row.data)
    check("Kontrolle: anderer post2-Kommentar noch da", db.get(models.Comments, p2_comments[1].id) is not None)

    # ------------------------------------------------------------------
    print("\n3) Story entfernen")
    vorher = archiv_anzahl()
    admin.remove_story(story_id, schemas.ContentRemove(reason="inappropriate"), db=db, current_user=mod)
    db.expire_all()

    check("genau EINE neue Archivzeile", archiv_anzahl() == vorher + 1)
    log, row = archiv_zu("delete_story")
    check("Metadaten", row is not None and row.target_type == "story" and row.target_id == story_id
          and row.owner_id == a.id and row.image_url == FAKE_URL)
    check("data = genau diese Story", row.data["id"] == story_id and row.data["image_url"] == FAKE_URL
          and set(row.data) == {c.name for c in models.Story.__table__.columns}, row.data)
    check("Original weg", db.get(models.Story, story_id) is None)

    # ------------------------------------------------------------------
    print("\n4) Profilbild entfernen")
    vorher = archiv_anzahl()
    admin.remove_profile_picture(a.id, schemas.ContentRemove(reason="inappropriate"), db=db, current_user=mod)
    db.expire_all()

    check("genau EINE neue Archivzeile", archiv_anzahl() == vorher + 1)
    log, row = archiv_zu("delete_profile_picture")
    check("Metadaten", row is not None and row.target_type == "profile_picture" and row.target_id is None
          and row.owner_id == a.id and row.image_url == FAKE_URL)
    check("data = nur die URL", row.data == {"profile_picture_url": FAKE_URL}, row.data)
    check("URL am User geleert", db.get(models.User, a.id).profile_picture_url is None)

    # ------------------------------------------------------------------
    print("\n5) Abgelehnte Entfernung archiviert nichts")
    adm_post = models.Post(title="Admin-Post", content="x", owner_id=adm.id)
    db.add(adm_post)
    db.commit()  # = Savepoint freigeben, bleibt in der aeusseren Transaktion
    vorher = archiv_anzahl()
    try:
        admin.remove_post(adm_post.id, schemas.ContentRemove(reason="spam"), db=db, current_user=mod)
        check("Mod -> Admin-Post abgelehnt", False, "kein Fehler")
    except HTTPException as e:
        check("Mod -> Admin-Post abgelehnt", e.status_code == 403, e.status_code)
    db.rollback()
    check("keine Archivzeile", archiv_anzahl() == vorher)

    # ------------------------------------------------------------------
    print("\n6) Gesamtbild")
    check("insgesamt genau 4 neue Archivzeilen", archiv_anzahl() == start_anzahl + 4, archiv_anzahl() - start_anzahl)
    check("S3 nie angefasst", s3_aufrufe == [], s3_aufrufe)

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
