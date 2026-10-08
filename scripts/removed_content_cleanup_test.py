"""Prueft Phase 4 removed_content: DELETE /cleanup/removed_content (Frist 180 Tage) und dass
delete_account die Archiv-Bilder mitnimmt. Laeuft gegen die ECHTE DB, komplett in einer
Transaktion, die am Ende zurueckgerollt wird (gleiches Muster wie snapshot_cleanup_test.py).

S3: die boto3-Clients in post/story/user werden durch EINEN Fake ersetzt, der nur mitschreibt.
So laeuft der echte Code (URL -> Bucket + Key), es wird aber nie wirklich etwas geloescht —
auch nicht fuer echte, schon abgelaufene Archivzeilen, die der Cleanup mit erfasst.
Keys mit "kaputt" wirft der Fake absichtlich -> Pfad ueber failed_image_deletions.

Start:  .\\venv\\Scripts\\python.exe scripts\\removed_content_cleanup_test.py
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
from app.routers import post as post_router, story as story_router, user as user_router  # noqa: E402
from app.routers.cleanup import clear_removed_content, SNAPSHOT_RETENTION_DAYS  # noqa: E402


class FakeS3:
    def __init__(self):
        self.aufrufe = []

    def delete_object(self, Bucket, Key):
        self.aufrufe.append((Bucket, Key))
        if "kaputt" in Key:
            raise RuntimeError("S3 simuliert kaputt")


fake = FakeS3()
post_router.s3_client = story_router.s3_client = user_router.s3_client = fake


def url(bucket, key):
    return f"https://example.invalid/storage/v1/object/public/{bucket}/{key}"


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
    owner = db.query(models.User).order_by(models.User.id).first()
    RC = models.RemovedContent
    jetzt = datetime.now(timezone.utc)
    alt = jetzt - timedelta(days=SNAPSHOT_RETENTION_DAYS + 1)
    knapp = jetzt - timedelta(days=SNAPSHOT_RETENTION_DAYS - 1)

    def archiv(user, target_type, image_url, removed_at):
        row = RC(target_type=target_type, target_id=None if target_type == "profile_picture" else 1,
                 owner_id=user.id, image_url=image_url, data={"test": True}, removed_at=removed_at)
        db.add(row)
        return row

    rows_alt = [archiv(owner, "post", url("post_images", "posts/alt.jpg"), alt),
                archiv(owner, "story", url("story_images", "alt.jpg"), alt),
                archiv(owner, "profile_picture", url("user_images", "profile_picture/alt.jpg"), alt),
                archiv(owner, "comment", None, alt),
                archiv(owner, "post", url("post_images", "posts/kaputt.jpg"), alt)]
    rows_frisch = [archiv(owner, "post", url("post_images", "posts/frisch.jpg"), jetzt),
                   archiv(owner, "story", url("story_images", "knapp.jpg"), knapp)]
    db.flush()
    alt_ids = {r.id for r in rows_alt}
    frisch_ids = {r.id for r in rows_frisch}
    # Echte, schon abgelaufene Archivzeilen nimmt der Cleanup mit -> Grundstand merken
    echte_abgelaufen = db.query(RC).filter(RC.removed_at < jetzt - timedelta(days=SNAPSHOT_RETENTION_DAYS),
                                           RC.id.notin_(alt_ids)).count()

    # ------------------------------------------------------------------
    print("1) Falsches Secret")
    try:
        clear_removed_content(db=db, x_cleanup_secret="falsch")
        check("401 bei falschem Secret", False)
    except HTTPException as e:
        check("401 bei falschem Secret", e.status_code == 401)

    # ------------------------------------------------------------------
    print("\n2) Aufraeumen")
    out = clear_removed_content(db=db, x_cleanup_secret=settings.story_cleanup_secret)
    db.expire_all()
    check(f"deleted = {len(alt_ids)} + {echte_abgelaufen} echte", out == {"deleted": len(alt_ids) + echte_abgelaufen}, out)
    check("alte Zeilen weg", db.query(RC).filter(RC.id.in_(alt_ids)).count() == 0)
    check("frische + knapp-unter-Frist bleiben", db.query(RC).filter(RC.id.in_(frisch_ids)).count() == 2)

    aufrufe = set(fake.aufrufe)
    check("Post-Bild im richtigen Bucket", ("post_images", "posts/alt.jpg") in aufrufe)
    check("Story-Bild im richtigen Bucket", ("story_images", "alt.jpg") in aufrufe)
    check("Profilbild im richtigen Bucket", ("user_images", "profile_picture/alt.jpg") in aufrufe)
    check("frische Bilder NICHT angefasst",
          ("post_images", "posts/frisch.jpg") not in aufrufe and ("story_images", "knapp.jpg") not in aufrufe)
    eigene = [a for a in fake.aufrufe if a[1] in {"posts/alt.jpg", "alt.jpg", "profile_picture/alt.jpg",
                                                  "posts/kaputt.jpg"}]
    check("genau 4 S3-Aufrufe fuer die eigenen Zeilen (Kommentar hat kein Bild)", len(eigene) == 4, eigene)

    gemerkt = db.query(models.FailedImageDeletions).filter(
        models.FailedImageDeletions.bucket == "post_images",
        models.FailedImageDeletions.s3_key == "posts/kaputt.jpg").count()
    check("S3-Fehler -> in failed_image_deletions gemerkt (Retry morgen)", gemerkt == 1, gemerkt)

    out2 = clear_removed_content(db=db, x_cleanup_secret=settings.story_cleanup_secret)
    check("zweiter Lauf: nichts mehr zu tun", out2 == {"deleted": 0}, out2)

    # ------------------------------------------------------------------
    print("\n3) delete_account nimmt Archiv-Bilder mit")
    tmp = models.User(email="archiv-test@example.invalid", passwort="x", username="archiv_test_tmp")
    db.add(tmp)
    db.flush()
    tmp_rows = [archiv(tmp, "post", url("post_images", "posts/tmp.jpg"), jetzt),
                archiv(tmp, "story", url("story_images", "tmp.jpg"), jetzt),
                archiv(tmp, "profile_picture", url("user_images", "profile_picture/tmp.jpg"), jetzt),
                archiv(tmp, "comment", None, jetzt)]
    db.flush()
    tmp_ids = {r.id for r in tmp_rows}
    fake.aufrufe.clear()

    user_router.delete_account(current_user=tmp, db=db)
    db.expire_all()
    check("alle 3 Archiv-Bilder geloescht, jeweils im richtigen Bucket",
          set(fake.aufrufe) == {("post_images", "posts/tmp.jpg"), ("story_images", "tmp.jpg"),
                                ("user_images", "profile_picture/tmp.jpg")}, fake.aufrufe)
    check("Archivzeilen per CASCADE weg", db.query(RC).filter(RC.id.in_(tmp_ids)).count() == 0)
    check("fremdes Archiv unangetastet", db.query(RC).filter(RC.id.in_(frisch_ids)).count() == 2)

finally:
    db.close()
    outer.rollback()  # ALLES verwerfen
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (DB unveraendert, Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
