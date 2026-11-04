"""Prueft die Auswahl im Tagesranking (GET /ranking/my_target): Filter, Notloesung,
Neuziehungen, Gewichtung und Post-Limit — gegen die ECHTE DB, komplett in einer Transaktion,
die am Ende zurueckgerollt wird (gleicher Trick wie group_roles_test.py). Das commit() in der
Route gibt dabei nur einen Savepoint frei.

Damit nur Test-User im Pool sind, setzt pool() ranking_enabled fuer alle anderen auf
False. Auch das rollt am Ende zurueck, andere Verbindungen sehen davon nichts. Das UPDATE
sperrt aber bis zum Ende alle users-Zeilen fuer andere Schreiber, das Skript soll also kurz
laufen. Deshalb rechnen die Haeufigkeits-Tests die vielen Ziehungen im Speicher, mit den
echten Zeilen aus _kandidaten_laden, statt tausendmal die Route aufzurufen.

Start:  .\\venv\\Scripts\\python.exe scripts\\daily_target_test.py
"""
import random
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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


def match_stand():
    """(target_user_id, rerolls) der einzigen Match-Zeile, frisch aus der DB."""
    zeile = db.query(models.DailyTarget.target_user_id, models.DailyTarget.rerolls).filter(
        models.DailyTarget.voter_id == voter.id).one()
    return zeile.target_user_id, zeile.rerolls


def fehlertext():
    """Ruft die Route auf und liefert den Fehlertext, oder None, wenn ein Target kam."""
    try:
        ranking.get_personal_target(db=db, current_user=voter)
        return None
    except HTTPException as e:
        return e.detail


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


def sperren(u):
    u.banned_until = JETZT + timedelta(days=3)
    db.flush()


# --- Gewichtung ---
SEED = 20261104
SPEICHER_ZIEHUNGEN = 10_000
TOLERANZ = 0.025  # 5 Standardabweichungen bei 10.000 Ziehungen und einem Anteil von 50 %
ROUTEN_ZIEHUNGEN = 40


class ZufallsSpion:
    """Merkt sich, welche Gewichte die Route an random.choices gibt, und zieht dann normal."""

    def __init__(self):
        self.gewichte = None

    def choices(self, kandidaten, weights, k):
        self.gewichte = list(weights)
        return random.choices(kandidaten, weights=weights, k=k)


def gewicht_von(ich=(None, None, None), vibes=(None, None), ort=None, heute=0, kuerzlich=False):
    """Reine Rechnung ohne DB. ich = (mein Vibe 1, mein Vibe 2, mein Ort)."""
    mein_profil = SimpleNamespace(vibe_factor_1=ich[0], vibe_factor_2=ich[1], location_id=ich[2])
    kandidat = SimpleNamespace(vibe_factor_1=vibes[0], vibe_factor_2=vibes[1], location_id=ort,
                               heute_gezogen=heute, kuerzlich_mein_target=kuerzlich)
    return ranking._target_gewicht(kandidat, mein_profil)


def profil(u, vibes=(None, None), ort=None):
    u.vibe_factor_1, u.vibe_factor_2 = vibes
    u.location_id = ort
    db.flush()


def paar(slug, posts_x=1):
    """Zwei frische Kandidaten ohne Vibes und Ort."""
    return neuer_user(f"{slug}_x", posts=posts_x), neuer_user(f"{slug}_y")


def von_anderen_gezogen(target, anzahl, tage_her=0):
    """Andere Voter hatten dieses Target."""
    for _ in range(anzahl):
        anderer = neuer_user(f"anderer_{secrets.token_hex(3)}", posts=0)
        db.add(models.DailyTarget(voter_id=anderer.id, target_user_id=target.id,
                                  date=date.today() - timedelta(days=tage_her)))
    db.flush()


def war_mein_target(target, tage_her):
    """Traegt ein altes Match von mir ein. Pro Tag gibt es nur eine Zeile, also vorher leeren."""
    tag_davor = date.today() - timedelta(days=tage_her)
    db.query(models.DailyTarget).filter(models.DailyTarget.voter_id == voter.id,
                                        models.DailyTarget.date == tag_davor).delete(
        synchronize_session=False)
    db.add(models.DailyTarget(voter_id=voter.id, target_user_id=target.id, date=tag_davor))
    db.flush()


def geladene_zeilen():
    """Die echte Kandidaten-Anweisung, nach ID sortiert: Die zufaellige Reihenfolge aus
    Postgres wuerde sonst den festen Seed aushebeln."""
    erlaubt = ranking._erlaubte_kandidaten(db, voter, JETZT - timedelta(days=7))
    zeilen = ranking._kandidaten_laden(db, erlaubt, voter, date.today())
    return sorted(zeilen, key=lambda k: k.id)


def anteil(name, x, y, erwartet):
    """Zieht oft aus x und y und vergleicht den Anteil von x mit dem erwarteten."""
    pool(x, y)
    zeilen = geladene_zeilen()
    gewichte = [ranking._target_gewicht(k, voter) for k in zeilen]
    random.seed(SEED)
    gezogen = random.choices(zeilen, weights=gewichte, k=SPEICHER_ZIEHUNGEN)
    gemessen = sum(1 for k in gezogen if k.id == x.id) / SPEICHER_ZIEHUNGEN
    check(name, abs(gemessen - erwartet) <= TOLERANZ, f"erwartet {erwartet:.3f}, gemessen {gemessen:.3f}")


def ziehe_nur_heute():
    """Wie ziehe(), aber aeltere Matches bleiben stehen."""
    db.query(models.DailyTarget).filter(models.DailyTarget.voter_id == voter.id,
                                        models.DailyTarget.date == date.today()).delete(
        synchronize_session=False)
    db.flush()
    return aufruf()


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
    check("alte Zeile ueberschrieben, nur ein Match", match_anzahl() == 1, f"Matches: {match_anzahl()}")
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

    print("\nHeutiges Target nachtraeglich gesperrt")
    spaeter_gesperrt = neuer_user("spaeter_gesperrt")
    pool(spaeter_gesperrt)
    check("vor der Sperre wird er gezogen", ziehe() == spaeter_gesperrt.id)
    pool(spaeter_gesperrt, ersatz)
    check("ohne Sperre bleibt das Target", aufruf() == spaeter_gesperrt.id)
    sperren(spaeter_gesperrt)
    check("gesperrt, noch nicht geswipt -> neu gezogen", aufruf() == ersatz.id)
    check("gesperrt: nur ein Match", match_anzahl() == 1, f"Matches: {match_anzahl()}")

    sperre_abgelaufen = neuer_user("sperre_abgelaufen")
    pool(sperre_abgelaufen, ersatz)
    db.query(models.DailyTarget).filter(models.DailyTarget.voter_id == voter.id).delete(
        synchronize_session=False)
    db.add(models.DailyTarget(voter_id=voter.id, target_user_id=sperre_abgelaufen.id))
    sperre_abgelaufen.banned_until = JETZT - timedelta(days=3)
    db.flush()
    check("abgelaufene Sperre -> Target bleibt", aufruf() == sperre_abgelaufen.id)

    einziger_gesperrt = neuer_user("einziger_gesperrt")
    pool(einziger_gesperrt)
    check("einziger Kandidat wird gezogen (Sperre)", ziehe() == einziger_gesperrt.id)
    sperren(einziger_gesperrt)
    check("gesperrt, niemand sonst da -> 404", aufruf() is None)

    print("\nSwipe direkt nach Meldung, Block oder Sperre, ohne neues my_target")
    def block_von_mir(u):
        db.add(models.Block(blocker_id=voter.id, blocked_id=u.id))
        db.flush()

    def block_gegen_mich(u):
        db.add(models.Block(blocker_id=u.id, blocked_id=voter.id))
        db.flush()

    for name, slug, aktion in (("gemeldet", "swipe_gemeldet", melden),
                               ("von mir geblockt", "swipe_von_mir", block_von_mir),
                               ("blockt mich", "swipe_gegen_mich", block_gegen_mich),
                               ("gesperrt", "swipe_gesperrt", sperren)):
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

    print("\nAbgelaufene Sperre haelt den Swipe nicht auf")
    pool(sperre_vorbei)
    check("Target gezogen (Sperre vorbei)", ziehe() == sperre_vorbei.id)
    status = swipe_status(sperre_vorbei)
    check("Sperre vorbei: Swipe -> 201", status == 201, str(status))
    db.query(models.RankingScores).filter(models.RankingScores.voter_id == voter.id).delete(
        synchronize_session=False)
    db.flush()

    print("\nNeuziehungen: hoechstens 2 pro Tag")
    r1, r2, r3, r4 = (neuer_user(f"reroll_{i}") for i in range(1, 5))
    pool(r1)
    check("Erstziehung", ziehe() == r1.id)
    check("Erstziehung zaehlt nicht", match_stand() == (r1.id, 0), str(match_stand()))
    check("erneuter Aufruf zaehlt nicht", aufruf() == r1.id and match_stand() == (r1.id, 0), str(match_stand()))

    pool(r1, r2)
    melden(r1)
    check("1. Neuziehung nach Meldung", aufruf() == r2.id)
    check("1. Neuziehung gezaehlt", match_stand() == (r2.id, 1), str(match_stand()))
    check("Aufruf danach zaehlt nicht", aufruf() == r2.id and match_stand() == (r2.id, 1), str(match_stand()))

    pool(r2, r3)
    block_von_mir(r2)
    check("2. Neuziehung nach Block", aufruf() == r3.id)
    check("2. Neuziehung gezaehlt", match_stand() == (r3.id, 2), str(match_stand()))
    check("gueltiges Target bleibt nach 2 Neuziehungen", aufruf() == r3.id and match_stand() == (r3.id, 2),
          str(match_stand()))

    pool(r3, r4)  # r4 waere frei, das 404 kommt also wirklich vom Limit
    sperren(r3)
    text = fehlertext()
    check("3. Neuziehung -> 404", aufruf() is None)
    check("Fehlertext nennt das Limit", text is not None and "Neuziehungen" in text, str(text))
    check("Zeile bleibt unveraendert", match_stand() == (r3.id, 2), str(match_stand()))
    check("nur ein Match", match_anzahl() == 1, f"Matches: {match_anzahl()}")

    print("\nNeuziehung ohne Kandidaten zaehlt nicht")
    k1 = neuer_user("kein_kandidat")
    pool(k1)
    check("Target gezogen (ohne Kandidaten)", ziehe() == k1.id)
    melden(k1)
    check("niemand sonst da -> 404", aufruf() is None)
    check("nichts gezaehlt", match_stand() == (k1.id, 0), str(match_stand()))
    k2 = neuer_user("spaeter_da")
    pool(k1, k2)
    check("spaeter wieder Kandidat da -> gezogen", aufruf() == k2.id)
    check("erst jetzt gezaehlt", match_stand() == (k2.id, 1), str(match_stand()))

    print("\nNeuziehung laesst andere Tage in Ruhe")
    t_alt = neuer_user("tag_alt")
    t_heute = neuer_user("tag_heute")
    t_neu = neuer_user("tag_neu")
    pool(t_heute)
    check("Target gezogen (Tage)", ziehe() == t_heute.id)
    vorgestern = models.DailyTarget(voter_id=voter.id, target_user_id=t_alt.id,
                                    date=date.today() - timedelta(days=2))
    db.add(vorgestern)
    db.flush()
    pool(t_heute, t_neu)
    melden(t_heute)
    check("Neuziehung heute", aufruf() == t_neu.id)
    db.refresh(vorgestern)
    check("alte Zeile behaelt ihr Target", vorgestern.target_user_id == t_alt.id)
    check("alte Zeile behaelt ihren Zaehler", vorgestern.rerolls == 0, str(vorgestern.rerolls))

    print("\nNach 2 Neuziehungen: Swipe und schon geswipt")
    s = neuer_user("swipe_limit")
    pool(s)
    check("Target gezogen (Limit)", ziehe() == s.id)
    db.query(models.DailyTarget).filter(models.DailyTarget.voter_id == voter.id).update(
        {models.DailyTarget.rerolls: 2}, synchronize_session=False)
    db.flush()
    db.expire_all()
    status = swipe_status(s)
    check("Limit erreicht: Swipe -> 201", status == 201, str(status))
    melden(s)
    text = fehlertext()
    check("geswipt + Limit: Meldung sagt abgeschlossen", text is not None and "abgeschlossen" in text, str(text))
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

    print("\nHeutiges Target gesperrt, schon geswipt")
    geswipt_gesperrt = neuer_user("geswipt_gesperrt")
    pool(geswipt_gesperrt)
    check("Target gezogen (Sperre)", ziehe() == geswipt_gesperrt.id)
    pool(geswipt_gesperrt, frei)
    sperren(geswipt_gesperrt)
    check("geswipt + gesperrt -> 404, kein zweites Target", aufruf() is None)
    check("Match bleibt bestehen (Sperre)", match_anzahl() == 1, f"Matches: {match_anzahl()}")

    print("\nGewicht: reine Rechnung")
    MEIN = ("vibe_a", "vibe_b", 7)
    VIBE = 1 + ranking.TARGET_VIBE_BONUS
    ORT = 1 + ranking.TARGET_LOCATION_BONUS
    BEIDES = 1 + ranking.TARGET_VIBE_BONUS + ranking.TARGET_LOCATION_BONUS
    KUERZLICH = ranking.TARGET_RECENT_FACTOR
    for name, gemessen, erwartet in (
        ("nichts gemeinsam", gewicht_von(MEIN, ("x", "y"), 8), 1),
        ("ein gemeinsamer Vibe", gewicht_von(MEIN, ("vibe_a", "y"), 8), VIBE),
        ("Vibe ueber Kreuz (mein 1 = sein 2)", gewicht_von(MEIN, ("y", "vibe_a"), 8), VIBE),
        ("zwei gemeinsame Vibes: Bonus nur einmal", gewicht_von(MEIN, ("vibe_a", "vibe_b"), 8), VIBE),
        ("gleicher Ort", gewicht_von(MEIN, ("x", "y"), 7), ORT),
        ("Vibe und Ort", gewicht_von(MEIN, ("vibe_a", "y"), 7), BEIDES),
        ("heute 1x gezogen", gewicht_von(MEIN, ("x", "y"), 8, heute=1), 1 / 2),
        ("heute 4x gezogen", gewicht_von(MEIN, ("x", "y"), 8, heute=4), 1 / 5),
        ("kuerzlich mein Target", gewicht_von(MEIN, ("x", "y"), 8, kuerzlich=True), KUERZLICH),
        ("alles zusammen", gewicht_von(MEIN, ("vibe_a", "y"), 7, heute=2, kuerzlich=True),
         BEIDES / 3 * KUERZLICH),
        ("alle Vibes leer", gewicht_von((None, None, 7), (None, None), 8), 1),
        ("meiner halb leer, seiner halb leer", gewicht_von(("vibe_a", None, 7), (None, "y"), 8), 1),
        ("meine Vibes leer, seine gesetzt", gewicht_von((None, None, 7), ("x", "y"), 8), 1),
        ("beide ohne Ort", gewicht_von(("a", "b", None), ("x", "y"), None), 1),
        ("nur ich ohne Ort", gewicht_von(("a", "b", None), ("x", "y"), 8), 1),
        ("nur er ohne Ort", gewicht_von(MEIN, ("x", "y"), None), 1),
    ):
        check(name, abs(gemessen - erwartet) < 1e-9, f"erwartet {erwartet}, gemessen {gemessen}")
    check("wird auch im schlimmsten Fall nicht 0", gewicht_von(MEIN, ("x", "y"), 8, heute=1000, kuerzlich=True) > 0)

    ort_ich = models.Location(name=f"dt_ort_ich_{tag}")
    ort_anders = models.Location(name=f"dt_ort_anders_{tag}")
    db.add_all([ort_ich, ort_anders])
    db.flush()
    MEINE_VIBES = ("vibe_a", "vibe_b")

    print("\nGewicht: Werte aus der echten Anweisung")
    profil(voter, MEINE_VIBES, ort_ich.id)
    w1 = neuer_user("werte_1", posts=5)
    profil(w1, ("vibe_a", "y"), ort_ich.id)
    von_anderen_gezogen(w1, 2)
    von_anderen_gezogen(w1, 1, tage_her=1)
    war_mein_target(w1, 14)
    w2 = neuer_user("werte_2")
    von_anderen_gezogen(w2, 1, tage_her=3)
    war_mein_target(w2, 15)
    w3 = neuer_user("werte_3")
    anderer_voter = neuer_user("werte_voter", posts=0)
    db.add(models.DailyTarget(voter_id=anderer_voter.id, target_user_id=w3.id,
                              date=date.today() - timedelta(days=3)))
    db.flush()
    pool(w1, w2, w3)
    zeilen = {k.id: k for k in geladene_zeilen()}
    check("jeder nur einmal, trotz 5 Posts", sorted(zeilen) == sorted([w1.id, w2.id, w3.id]), str(sorted(zeilen)))
    z1, z2, z3 = zeilen[w1.id], zeilen[w2.id], zeilen[w3.id]
    check("Vibes geladen", (z1.vibe_factor_1, z1.vibe_factor_2) == ("vibe_a", "y"))
    check("Ort geladen", z1.location_id == ort_ich.id)
    check("heute gezogen: nur heute, ueber alle Voter", z1.heute_gezogen == 2, str(z1.heute_gezogen))
    check("heute gezogen: keiner", z2.heute_gezogen == 0, str(z2.heute_gezogen))
    check("mein Target vor genau 14 Tagen zaehlt", z1.kuerzlich_mein_target is True)
    check("mein Target vor 15 Tagen zaehlt nicht", z2.kuerzlich_mein_target is False)
    check("Target eines anderen Voters zaehlt nicht", z3.kuerzlich_mein_target is False)

    print("\nHaeufigkeit: gemeinsame Vibes und Ort kommen oefter")
    x, y = paar("vibe")
    profil(x, ("vibe_a", "z"))
    profil(y, ("q", "z"))
    anteil("gemeinsamer Vibe", x, y, VIBE / (VIBE + 1))
    x, y = paar("beide_vibes")
    profil(x, MEINE_VIBES)
    anteil("zwei gemeinsame Vibes: nicht doppelt", x, y, VIBE / (VIBE + 1))
    x, y = paar("ort")
    profil(x, ort=ort_ich.id)
    profil(y, ort=ort_anders.id)
    anteil("gleicher Ort", x, y, ORT / (ORT + 1))
    x, y = paar("vibe_ort")
    profil(x, ("vibe_b", "z"), ort_ich.id)
    anteil("Vibe und Ort", x, y, BEIDES / (BEIDES + 1))

    print("\nHaeufigkeit: oft Gezogene und kuerzliche Targets kommen seltener")
    x, y = paar("oft")
    von_anderen_gezogen(x, 1)
    anteil("heute 1x gezogen", x, y, (1 / 2) / (1 / 2 + 1))
    x, y = paar("sehr_oft")
    von_anderen_gezogen(x, 4)
    anteil("heute 4x gezogen", x, y, (1 / 5) / (1 / 5 + 1))
    x, y = paar("gestern")
    von_anderen_gezogen(x, 4, tage_her=1)
    anteil("gestern gezogen zaehlt nicht", x, y, 1 / 2)
    x, y = paar("kuerzlich")
    war_mein_target(x, 3)
    anteil("kuerzlich mein Target", x, y, KUERZLICH / (KUERZLICH + 1))
    x, y = paar("vor_14")
    war_mein_target(x, 14)
    anteil("mein Target vor 14 Tagen", x, y, KUERZLICH / (KUERZLICH + 1))
    x, y = paar("vor_15")
    war_mein_target(x, 15)
    anteil("mein Target vor 15 Tagen zaehlt nicht", x, y, 1 / 2)
    x, y = paar("fremd")
    db.add(models.DailyTarget(voter_id=anderer_voter.id, target_user_id=x.id,
                              date=date.today() - timedelta(days=5)))
    db.flush()
    anteil("Target eines anderen Voters zaehlt nicht", x, y, 1 / 2)

    print("\nHaeufigkeit: leere Werte sind kein Treffer")
    profil(voter, ("vibe_a", None), ort_ich.id)
    x, y = paar("leer_vibe_1")  # x hat keine Vibes, wuerde bei None == None profitieren
    profil(y, ("q", "z"))
    anteil("mein Vibe 2 leer, seine beide leer", x, y, 1 / 2)
    profil(voter, (None, None), ort_ich.id)
    x, y = paar("leer_vibe_2")
    profil(y, ("q", "z"))
    anteil("meine Vibes leer, seine beide leer", x, y, 1 / 2)
    profil(voter, MEINE_VIBES, None)
    x, y = paar("leer_ort")
    profil(y, ort=ort_anders.id)
    anteil("ich ohne Ort, er ohne Ort", x, y, 1 / 2)
    profil(voter, MEINE_VIBES, ort_ich.id)

    print("\nHaeufigkeit: mehr Posts bringen keine bessere Chance")
    x, y = paar("viele_posts_chance", posts_x=5)
    anteil("5 Posts gegen 1 Post", x, y, 1 / 2)
    x, y = paar("viele_posts_zaehler", posts_x=5)
    von_anderen_gezogen(x, 1)
    anteil("5 Posts blaehen 'heute gezogen' nicht auf", x, y, (1 / 2) / (1 / 2 + 1))

    print("\nKandidaten-Limit")
    limit_vorher = ranking.TARGET_MAX_CANDIDATES
    ranking.TARGET_MAX_CANDIDATES = 3
    try:
        pool(*(neuer_user(f"limit_{i}") for i in range(5)))
        zeilen = geladene_zeilen()
        check("hoechstens TARGET_MAX_CANDIDATES", len(zeilen) == 3, f"geladen: {len(zeilen)}")
        check("keine doppelten", len({k.id for k in zeilen}) == len(zeilen))
    finally:
        ranking.TARGET_MAX_CANDIDATES = limit_vorher

    print("\nDie Route zieht wirklich gewichtet")
    x, y = paar("route")
    profil(x, ("vibe_a", "z"), ort_ich.id)
    pool(x, y)
    zufall_vorher = ranking.random
    spion = ZufallsSpion()
    ranking.random = spion
    try:
        ziehe()
        check("Route gibt die richtigen Gewichte weiter", sorted(spion.gewichte) == [1, BEIDES], str(spion.gewichte))

        folge_x, folge_y = paar("route_notloesung")
        profil(folge_x, ("vibe_a", "z"))
        folgen(folge_x)
        folgen(folge_y)
        pool(folge_x, folge_y)
        ziehe()
        check("Notloesung zieht auch gewichtet", sorted(spion.gewichte) == [1, VIBE], str(spion.gewichte))
    finally:
        ranking.random = zufall_vorher

    pool(x, y)
    gezogen = [ziehe() for _ in range(ROUTEN_ZIEHUNGEN)]
    treffer = gezogen.count(x.id)
    check("nur erlaubte Kandidaten", set(gezogen) <= {x.id, y.id}, str(set(gezogen)))
    # Erwartet sind 33 von 40 Treffern, unter 20 ist praktisch ausgeschlossen.
    check("der Staerkere gewinnt deutlich oefter", treffer >= 20, f"{treffer} von {ROUTEN_ZIEHUNGEN}")
    target_zeile, _ = match_stand()
    check("gezogenes Target ist gespeichert", target_zeile == gezogen[-1])

    print("\nKleines Gewicht heisst nicht ausgeschlossen")
    x, y = paar("einziger_kuerzlich")
    war_mein_target(x, 2)
    von_anderen_gezogen(x, 3)
    pool(x)
    check("kuerzliches Target als einziger Kandidat wird gezogen", ziehe_nur_heute() == x.id)
    gefolgt_stark = neuer_user("gefolgt_stark")
    profil(gefolgt_stark, ("vibe_a", "z"), ort_ich.id)
    folgen(gefolgt_stark)
    pool(x, gefolgt_stark)
    gezogen = {ziehe_nur_heute() for _ in range(ZIEHUNGEN)}
    check("schwacher Nicht-Gefolgter schlaegt starken Gefolgten", gezogen == {x.id}, str(gezogen))

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
