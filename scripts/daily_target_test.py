"""Prueft die Auswahl im Tagesranking (GET /ranking/my_target): Filter, Notloesung und
Post-Limit — gegen die ECHTE DB, komplett in einer Transaktion, die am Ende
zurueckgerollt wird (gleicher Trick wie group_roles_test.py). Das commit() in der
Route gibt dabei nur einen Savepoint frei.

Damit nur Test-User im Pool sind, setzt pool() ranking_enabled fuer alle anderen auf
False. Auch das rollt am Ende zurueck, andere Verbindungen sehen davon nichts.

Start:  .\\venv\\Scripts\\python.exe scripts\\daily_target_test.py
"""
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models, schemas, utils  # noqa: E402
from app.database import engine  # noqa: E402
from app.routers import ranking  # noqa: E402

fehler = 0


class RedisAttrappe:
    """Ein erfolgreicher Swipe schreibt ins Live-Leaderboard. Das laesst sich nicht
    zurueckrollen, und Test-User im Leaderboard wuerden dort Fehler ausloesen."""

    def __init__(self):
        self.zincrby_aufrufe = 0

    def zincrby(self, key, punkte, user_id):
        self.zincrby_aufrufe += 1

    def expire(self, key, sekunden):
        pass


redis_attrappe = RedisAttrappe()
ranking.redis_client = redis_attrappe


def check(name, bedingung, info=""):
    global fehler
    print(f"  {'OK ' if bedingung else 'FEHLER'}  {name}  {info}")
    if not bedingung:
        fehler += 1


conn = engine.connect()
outer = conn.begin()
db = Session(bind=conn, join_transaction_mode="create_savepoint")

PW_HASH = utils.hash_password("test-pw-12345")
tag = secrets.token_hex(4)
JETZT = datetime.now(timezone.utc)
ZIEHUNGEN = 15


def neuer_user(name, posts=1, post_alter_tage=0):
    u = models.User(email=f"dt_{name}_{tag}@example.com", username=f"dt_{name}_{tag}",
                    passwort=PW_HASH, email_verified=True, ranking_enabled=True)
    db.add(u)
    db.flush()
    for i in range(posts):
        db.add(models.Post(owner_id=u.id, title=f"t{i}", content="c",
                           created_at=JETZT - timedelta(days=post_alter_tage, minutes=i)))
    db.flush()
    return u


def pool(*user):
    """Nur der Voter und diese User nehmen am Ranking teil."""
    ids = [voter.id] + [u.id for u in user]
    db.query(models.User).filter(models.User.id.notin_(ids)).update(
        {models.User.ranking_enabled: False}, synchronize_session=False)
    db.query(models.User).filter(models.User.id.in_(ids)).update(
        {models.User.ranking_enabled: True}, synchronize_session=False)
    db.flush()
    db.expire_all()


def aufruf():
    """Ruft die Route auf, ohne das heutige Match anzufassen.
    Liefert die Target-ID oder None bei 404."""
    try:
        return ranking.get_personal_target(db=db, current_user=voter)["user_data"].id
    except HTTPException as e:
        if e.status_code == 404:
            return None
        raise


def ziehe():
    """Eine frische Ziehung: das heutige Match wegwerfen, dann die Route aufrufen."""
    db.query(models.DailyTarget).filter(models.DailyTarget.voter_id == voter.id).delete(
        synchronize_session=False)
    db.flush()
    return aufruf()


def match_anzahl():
    return db.query(models.DailyTarget).filter(models.DailyTarget.voter_id == voter.id).count()


def swipe_status(target):
    """Swipt einen Post des Targets. Liefert 201 oder den Fehlercode."""
    post = db.query(models.Post).filter(models.Post.owner_id == target.id).first()
    session = schemas.SwipeSession(target_user_id=target.id,
                                   swipes=[{"post_id": post.id, "direction": True}])
    try:
        ranking.swipe_session(session=session, db=db, current_user=voter)
        return 201
    except HTTPException as e:
        return e.status_code


def score_anzahl():
    return db.query(models.RankingScores).filter(models.RankingScores.voter_id == voter.id).count()


def immer(name, erwartet, *user):
    pool(*user)
    gezogen = {ziehe() for _ in range(ZIEHUNGEN)}
    check(name, gezogen == {erwartet}, f"gezogen: {gezogen}")


def melden(gegen, target_type="user", target_id=None, status="pending"):
    db.add(models.Report(reporter_id=voter.id, reported_user_id=gegen.id, target_type=target_type,
                         target_id=target_id, reason="spam", status=status))
    db.flush()


def folgen(wem):
    db.add(models.Follows(follower_id=voter.id, followee_id=wem.id))
    db.flush()


try:
    voter = neuer_user("voter")
    frei = neuer_user("frei")

    print("\nGrundregeln")
    immer("freier Kandidat wird gezogen", frei.id, frei)

    pool(frei)
    ziehe()
    a = ranking.get_personal_target(db=db, current_user=voter)["user_data"].id
    b = ranking.get_personal_target(db=db, current_user=voter)["user_data"].id
    check("gleicher Tag -> gleiches Target", a == b == frei.id)

    pool()
    check("leerer Pool -> 404, nie man selbst", ziehe() is None)

    ohne_optin = neuer_user("ohne_optin")
    pool()  # ohne_optin bleibt ausserhalb, also ranking_enabled=False
    check("ohne ranking_enabled -> 404", ziehe() is None)

    alt = neuer_user("alt", post_alter_tage=8)
    pool(alt)
    check("nur Posts aelter als 7 Tage -> 404", ziehe() is None)

    print("\nGefolgte und Notloesung")
    gefolgt = neuer_user("gefolgt")
    folgen(gefolgt)
    immer("Nicht-Follower geht vor Gefolgtem", frei.id, gefolgt, frei)
    immer("nur Gefolgter da -> Notloesung greift", gefolgt.id, gefolgt)

    print("\nBlocks")
    ich_blocke = neuer_user("ich_blocke")
    db.add(models.Block(blocker_id=voter.id, blocked_id=ich_blocke.id))
    er_blockt = neuer_user("er_blockt")
    db.add(models.Block(blocker_id=er_blockt.id, blocked_id=voter.id))
    db.flush()
    immer("von mir Geblockter nie gezogen", frei.id, ich_blocke, frei)
    immer("wer mich blockt, nie gezogen", frei.id, er_blockt, frei)
    pool(ich_blocke)
    check("Geblockter als einziger -> 404", ziehe() is None)
    pool(er_blockt)
    check("Blockierender als einziger -> 404", ziehe() is None)

    print("\nSperren")
    gesperrt = neuer_user("gesperrt")
    gesperrt.banned_until = JETZT + timedelta(days=3)
    sperre_vorbei = neuer_user("sperre_vorbei")
    sperre_vorbei.banned_until = JETZT - timedelta(days=3)
    db.flush()
    immer("aktuell Gesperrter nie gezogen", frei.id, gesperrt, frei)
    pool(gesperrt)
    check("Gesperrter als einziger -> 404", ziehe() is None)
    immer("abgelaufene Sperre -> wieder im Pool", sperre_vorbei.id, sperre_vorbei)

    print("\nGemeldete: niemals, auch nicht als einziges Target")
    gemeldet_user = neuer_user("gemeldet_user")
    melden(gemeldet_user)
    gemeldet_post = neuer_user("gemeldet_post")
    melden(gemeldet_post, target_type="post", target_id=1)
    gemeldet_abgelehnt = neuer_user("gemeldet_abgelehnt")
    melden(gemeldet_abgelehnt, status="dismissed")
    immer("gemeldeter User nie gezogen", frei.id, gemeldet_user, frei)
    for name, u in (("Meldung gegen den User", gemeldet_user),
                    ("Meldung gegen einen Post", gemeldet_post),
                    ("abgelehnte Meldung", gemeldet_abgelehnt)):
        pool(u)
        check(f"{name}, einziger Kandidat -> 404", ziehe() is None)

    gefolgt_gemeldet = neuer_user("gefolgt_gemeldet")
    folgen(gefolgt_gemeldet)
    melden(gefolgt_gemeldet)
    pool(gefolgt_gemeldet)
    check("gemeldet + gefolgt -> auch Notloesung liefert 404", ziehe() is None)

    gefolgt_geblockt = neuer_user("gefolgt_geblockt")
    folgen(gefolgt_geblockt)
    db.add(models.Block(blocker_id=voter.id, blocked_id=gefolgt_geblockt.id))
    db.flush()
    pool(gefolgt_geblockt)
    check("geblockt + gefolgt -> auch Notloesung liefert 404", ziehe() is None)

    print("\nPost-Limit")
    viele_posts = neuer_user("viele_posts", posts=25)
    pool(viele_posts)
    ziehe()
    ergebnis = ranking.get_personal_target(db=db, current_user=voter)
    anzahl = len(ergebnis["posts"])
    check("25 Posts -> hoechstens 20 geliefert", anzahl == 20, f"geliefert: {anzahl}")
    zeiten = [p.created_at for p in ergebnis["posts"]]
    check("neueste zuerst", zeiten == sorted(zeiten, reverse=True))

    print("\nHeutiges Target nachtraeglich gemeldet")
    spaeter_gemeldet = neuer_user("spaeter_gemeldet")
    ersatz = neuer_user("ersatz")
    pool(spaeter_gemeldet)
    check("vor der Meldung wird er gezogen", ziehe() == spaeter_gemeldet.id)
    pool(spaeter_gemeldet, ersatz)
    check("ohne Meldung bleibt das Target", aufruf() == spaeter_gemeldet.id)
    alter_post = db.query(models.Post).filter(models.Post.owner_id == spaeter_gemeldet.id).first()
    melden(spaeter_gemeldet)
    check("noch nicht geswipt -> neu gezogen", aufruf() == ersatz.id)
    check("alte Zeile ist weg, nur ein Match", match_anzahl() == 1, f"Matches: {match_anzahl()}")
    check("zweiter Aufruf bleibt beim Ersatz", aufruf() == ersatz.id)
    try:
        ranking.swipe_session(
            session=schemas.SwipeSession(target_user_id=spaeter_gemeldet.id,
                                         swipes=[{"post_id": alter_post.id, "direction": True}]),
            db=db, current_user=voter)
        check("Swipe aufs alte Target -> 404", False, "kein Fehler geworfen")
    except HTTPException as e:
        check("Swipe aufs alte Target -> 404", e.status_code == 404, str(e.status_code))

    gemeldet_per_post = neuer_user("gemeldet_per_post")
    pool(gemeldet_per_post, ersatz)
    db.query(models.DailyTarget).filter(models.DailyTarget.voter_id == voter.id).delete(
        synchronize_session=False)
    db.add(models.DailyTarget(voter_id=voter.id, target_user_id=gemeldet_per_post.id))
    db.flush()
    melden(gemeldet_per_post, target_type="post", target_id=1)
    check("Meldung gegen einen Post zaehlt auch", aufruf() == ersatz.id)

    einziger = neuer_user("einziger")
    pool(einziger)
    check("einziger Kandidat wird gezogen", ziehe() == einziger.id)
    melden(einziger)
    check("gemeldet, niemand sonst da -> 404", aufruf() is None)

    print("\nHeutiges Target nachtraeglich geblockt")
    for name, slug, ich_blocke_ihn in (("von mir geblockt", "spaeter_von_mir", True),
                                       ("blockt mich", "spaeter_gegen_mich", False)):
        u = neuer_user(slug)
        pool(u)
        check(f"{name}: vorher wird er gezogen", ziehe() == u.id)
        pool(u, ersatz)
        check(f"{name}: ohne Block bleibt das Target", aufruf() == u.id)
        if ich_blocke_ihn:
            db.add(models.Block(blocker_id=voter.id, blocked_id=u.id))
        else:
            db.add(models.Block(blocker_id=u.id, blocked_id=voter.id))
        db.flush()
        check(f"{name}: noch nicht geswipt -> neu gezogen", aufruf() == ersatz.id)
        check(f"{name}: nur ein Match", match_anzahl() == 1, f"Matches: {match_anzahl()}")

    einziger_geblockt = neuer_user("einziger_geblockt")
    pool(einziger_geblockt)
    check("einziger Kandidat wird gezogen (Block)", ziehe() == einziger_geblockt.id)
    db.add(models.Block(blocker_id=voter.id, blocked_id=einziger_geblockt.id))
    db.flush()
    check("geblockt, niemand sonst da -> 404", aufruf() is None)

    print("\nSwipe direkt nach Meldung oder Block, ohne neues my_target")
    def block_von_mir(u):
        db.add(models.Block(blocker_id=voter.id, blocked_id=u.id))
        db.flush()

    def block_gegen_mich(u):
        db.add(models.Block(blocker_id=u.id, blocked_id=voter.id))
        db.flush()

    for name, slug, aktion in (("gemeldet", "swipe_gemeldet", melden),
                               ("von mir geblockt", "swipe_von_mir", block_von_mir),
                               ("blockt mich", "swipe_gegen_mich", block_gegen_mich)):
        u = neuer_user(slug)
        pool(u)
        check(f"{name}: Target gezogen", ziehe() == u.id)
        aktion(u)
        xp_vorher = u.xp
        status = swipe_status(u)
        check(f"{name}: Swipe -> 404", status == 404, str(status))
        check(f"{name}: keine Punkte gespeichert", score_anzahl() == 0, f"Scores: {score_anzahl()}")
        check(f"{name}: kein XP fuers Target", u.xp == xp_vorher, f"{xp_vorher} -> {u.xp}")
    check("nichts im Leaderboard gelandet", redis_attrappe.zincrby_aufrufe == 0,
          f"Aufrufe: {redis_attrappe.zincrby_aufrufe}")

    print("\nNormaler Swipe geht weiter durch")
    normal = neuer_user("swipe_normal")
    pool(normal)
    check("Target gezogen (normal)", ziehe() == normal.id)
    status = swipe_status(normal)
    check("Swipe -> 201", status == 201, str(status))
    check("Punkte gespeichert", score_anzahl() == 1, f"Scores: {score_anzahl()}")
    check("Leaderboard aktualisiert", redis_attrappe.zincrby_aufrufe == 1,
          f"Aufrufe: {redis_attrappe.zincrby_aufrufe}")
    db.query(models.RankingScores).filter(models.RankingScores.voter_id == voter.id).delete(
        synchronize_session=False)
    db.flush()

    print("\nHeutiges Target gemeldet, schon geswipt")
    geswipt = neuer_user("geswipt")
    pool(geswipt)
    check("Target gezogen", ziehe() == geswipt.id)
    post = db.query(models.Post).filter(models.Post.owner_id == geswipt.id).first()
    db.add(models.RankingScores(voter_id=voter.id, post_id=post.id, direction=True, points=1))
    db.flush()
    check("geswipt, nicht gemeldet -> gleiches Target", aufruf() == geswipt.id)
    pool(geswipt, frei)
    melden(geswipt)
    check("geswipt + gemeldet -> 404, kein zweites Target", aufruf() is None)
    check("Match bleibt bestehen", match_anzahl() == 1, f"Matches: {match_anzahl()}")

    print("\nHeutiges Target geblockt, schon geswipt")
    geswipt_geblockt = neuer_user("geswipt_geblockt")
    pool(geswipt_geblockt)
    check("Target gezogen (Block)", ziehe() == geswipt_geblockt.id)
    pool(geswipt_geblockt, frei)
    db.add(models.Block(blocker_id=geswipt_geblockt.id, blocked_id=voter.id))
    db.flush()
    check("geswipt + geblockt -> 404, kein zweites Target", aufruf() is None)
    check("Match bleibt bestehen (Block)", match_anzahl() == 1, f"Matches: {match_anzahl()}")

    print("\nVoter ohne Opt-in")
    voter.ranking_enabled = False
    db.flush()
    try:
        ranking.get_personal_target(db=db, current_user=voter)
        check("Voter ohne ranking_enabled -> 403", False, "kein Fehler geworfen")
    except HTTPException as e:
        check("Voter ohne ranking_enabled -> 403", e.status_code == 403, str(e.status_code))

finally:
    db.close()
    outer.rollback()
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'} (Transaktion zurueckgerollt)")
sys.exit(1 if fehler else 0)
