"""Prueft POST /profile_ranking/posts/{post_id} (Post ins Profil-Ranking stellen) gegen die
ECHTE DB, komplett in einer Transaktion, die am Ende zurueckgerollt wird (gleicher Trick wie
daily_target_test.py). Das commit() in der Route gibt dabei nur einen Savepoint frei.

Geprueft werden: die vier Pruefungen und ihre Reihenfolge (404 -> 403 -> 409 -> 400), die
7-Tage-Grenze, die Tagessperre (1 Post pro Tag, gestern belegt = heute frei), dass ein
Fehlschlag nichts veraendert und dass die Sperre nur den eigenen User trifft.

Dazu POST /profile_ranking/swipes: 404 (fehlt/nicht im Ranking) -> 403 eigener Post ->
403 kein Follower -> 403 Block (beide Richtungen) -> 409 Frist abgelaufen, erster und letzter
Tag, 409 schon geswipt (keine Ruecknahme), Like +1, Ueberspringen 0, keine ranking_scores-Zeile,
XP und Streak unveraendert.

Dazu GET /open, GET /posts und die Profilzahl ranking_likes_total in GET /users/ und
GET /users/{id}: nur Ranking-Likes, 0 statt None, Swipes wirken, je EINE Anweisung.

Nicht geprueft: zwei echt gleichzeitige Anfragen. Dafuer braeuchte es zwei Verbindungen mit
echtem Commit auf Prod. Abgesichert ist das durch das UPDATE ... WHERE in einer Anweisung.

Start:  .\\venv\\Scripts\\python.exe scripts\\profile_ranking_test.py
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
from app.ranking_config import PROFILE_RANKING_DAYS  # noqa: E402
from app.routers import profile_ranking  # noqa: E402

fehler = 0


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
HEUTE = JETZT.date()
GESTERN = HEUTE - timedelta(days=1)


def neuer_user(name):
    u = models.User(email=f"pr_{name}_{tag}@example.com", username=f"pr_{name}_{tag}",
                    passwort=PW_HASH, email_verified=True)
    db.add(u)
    db.flush()
    return u


def neuer_post(owner, alter=timedelta(0)):
    p = models.Post(owner_id=owner.id, title="t", content="c", created_at=JETZT - alter)
    db.add(p)
    db.flush()
    return p


def hinzufuegen(post_id, user):
    """Ruft die Route auf. Liefert (201, Antwort) oder (Fehlercode, Fehlertext)."""
    try:
        antwort = profile_ranking.add_post_to_ranking(post_id=post_id, db=db, current_user=user)
        return 201, antwort
    except HTTPException as e:
        return e.status_code, e.detail


def eintrag_von(post):
    """ranking_entered_on frisch aus der DB."""
    db.expire_all()
    return db.query(models.Post.ranking_entered_on).filter(models.Post.id == post.id).scalar()


def sperre_von(user):
    """last_ranking_entry_on frisch aus der DB."""
    db.expire_all()
    return db.query(models.User.last_ranking_entry_on).filter(models.User.id == user.id).scalar()


def sperre_setzen(user, datum):
    db.query(models.User).filter(models.User.id == user.id).update(
        {models.User.last_ranking_entry_on: datum}, synchronize_session=False)
    db.flush()


TEXT_SCHON_DRIN = "Dieser Post ist schon im Ranking."
TEXT_TAGESPLATZ = "Du hast heute schon einen Post ins Ranking gestellt."

try:
    anna = neuer_user("anna")
    ben = neuer_user("ben")

    # ---------------------------------------------------------------
    print("\n[0] Route ist in der App eingebunden")
    from app.main import app  # noqa: E402
    pfade = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
    check("POST /profile_ranking/posts/{post_id} existiert",
          ("/profile_ranking/posts/{post_id}", "POST") in pfade)

    # ---------------------------------------------------------------
    print("\n[1] 404: Post existiert nicht")
    hoechste_id = db.query(models.Post.id).order_by(models.Post.id.desc()).limit(1).scalar() or 0
    code, text = hinzufuegen(hoechste_id + 1000, anna)
    check("404", code == 404, f"({code} {text})")
    check("Sperre unveraendert (leer)", sperre_von(anna) is None)

    # ---------------------------------------------------------------
    print("\n[2] 403: fremder Post")
    bens_post = neuer_post(ben)
    code, text = hinzufuegen(bens_post.id, anna)
    check("403", code == 403, f"({code} {text})")
    check("Bens Post nicht im Ranking", eintrag_von(bens_post) is None)
    check("Annas Sperre unveraendert", sperre_von(anna) is None)
    check("Bens Sperre unveraendert", sperre_von(ben) is None)

    # ---------------------------------------------------------------
    print(f"\n[3] 400: Post aelter als {PROFILE_RANKING_DAYS} Tage")
    zu_alt = neuer_post(anna, timedelta(days=PROFILE_RANKING_DAYS, minutes=1))
    code, text = hinzufuegen(zu_alt.id, anna)
    check("400", code == 400, f"({code} {text})")
    check("Post nicht im Ranking", eintrag_von(zu_alt) is None)
    check("Sperre unveraendert (leer)", sperre_von(anna) is None)

    # ---------------------------------------------------------------
    print(f"\n[4] Grenze: knapp unter {PROFILE_RANKING_DAYS} Tagen geht noch")
    knapp = neuer_post(anna, timedelta(days=PROFILE_RANKING_DAYS) - timedelta(minutes=1))
    code, antwort = hinzufuegen(knapp.id, anna)
    check("201", code == 201, f"({code} {antwort})")
    check("Antwort enthaelt post_id", code == 201 and antwort.get("post_id") == knapp.id)
    check("Antwort enthaelt heutiges Datum",
          code == 201 and antwort.get("ranking_entered_on") == HEUTE)
    check("ranking_entered_on = heute (UTC)", eintrag_von(knapp) == HEUTE)
    check("last_ranking_entry_on = heute (UTC)", sperre_von(anna) == HEUTE)

    # ---------------------------------------------------------------
    print("\n[5] 409: derselbe Post noch einmal")
    code, text = hinzufuegen(knapp.id, anna)
    check("409", code == 409, f"({code} {text})")
    check("Grund: schon im Ranking", text == TEXT_SCHON_DRIN, f"({text})")
    check("ranking_entered_on bleibt heute", eintrag_von(knapp) == HEUTE)

    # ---------------------------------------------------------------
    print("\n[6] 409: zweiter eigener Post am selben Tag (Tagesplatz belegt)")
    zweiter = neuer_post(anna)
    code, text = hinzufuegen(zweiter.id, anna)
    check("409", code == 409, f"({code} {text})")
    check("Grund: Tagesplatz", text == TEXT_TAGESPLATZ, f"({text})")
    check("zweiter Post nicht im Ranking", eintrag_von(zweiter) is None)
    check("Sperre bleibt heute", sperre_von(anna) == HEUTE)

    # ---------------------------------------------------------------
    print("\n[7] Reihenfolge der Pruefungen")
    # fremder Post, der schon im Ranking ist -> 403 vor 409
    code, text = hinzufuegen(knapp.id, ben)
    check("fremd + schon drin -> 403", code == 403, f"({code} {text})")
    # zu alter Post, der schon im Ranking ist -> 409 vor 400
    alt_und_drin = neuer_post(anna, timedelta(days=PROFILE_RANKING_DAYS + 3))
    db.query(models.Post).filter(models.Post.id == alt_und_drin.id).update(
        {models.Post.ranking_entered_on: HEUTE - timedelta(days=3)}, synchronize_session=False)
    db.flush()
    code, text = hinzufuegen(alt_und_drin.id, anna)
    check("schon drin + zu alt -> 409 (schon drin)",
          code == 409 and text == TEXT_SCHON_DRIN, f"({code} {text})")
    # zu alter Post bei belegtem Tagesplatz -> 400 vor 409 Tagesplatz
    code, text = hinzufuegen(zu_alt.id, anna)
    check("zu alt + Tagesplatz belegt -> 400", code == 400, f"({code} {text})")

    # ---------------------------------------------------------------
    print("\n[8] Sperre gilt nur fuer den eigenen User")
    code, antwort = hinzufuegen(bens_post.id, ben)
    check("Ben darf heute trotzdem", code == 201, f"({code} {antwort})")
    check("Bens Post im Ranking", eintrag_von(bens_post) == HEUTE)
    check("Bens Sperre = heute", sperre_von(ben) == HEUTE)
    check("Annas zweiter Post weiter nicht drin", eintrag_von(zweiter) is None)

    # ---------------------------------------------------------------
    print("\n[9] Gestern belegt -> heute wieder frei")
    sperre_setzen(anna, GESTERN)
    code, antwort = hinzufuegen(zweiter.id, anna)
    check("201", code == 201, f"({code} {antwort})")
    check("zweiter Post jetzt im Ranking", eintrag_von(zweiter) == HEUTE)
    check("Sperre springt auf heute", sperre_von(anna) == HEUTE)
    check("erster Post bleibt im Ranking", eintrag_von(knapp) == HEUTE)

    # ---------------------------------------------------------------
    print("\n[10] Sperre in der Zukunft (z. B. Uhrzeit-Sprung) blockiert auch")
    dritter = neuer_post(anna)
    sperre_setzen(anna, HEUTE + timedelta(days=1))
    code, text = hinzufuegen(dritter.id, anna)
    check("409 Tagesplatz", code == 409 and text == TEXT_TAGESPLATZ, f"({code} {text})")
    check("dritter Post nicht im Ranking", eintrag_von(dritter) is None)

    # ===============================================================
    # POST /profile_ranking/swipes
    # ===============================================================
    # Eigene Darsteller, damit nichts aus [1]-[10] reinspielt:
    #   olga  = Besitzerin der Ranking-Posts
    #   finn  = folgt olga (darf swipen)
    #   fiona = folgt olga (zweite Stimme fuer den Zaehler)
    #   kai   = folgt olga NICHT, aber olga folgt kai (Richtung zaehlt)
    #   bea   = folgt olga, olga hat bea blockiert
    #   bob   = folgt olga, bob hat olga blockiert
    olga = neuer_user("olga")
    finn = neuer_user("finn")
    fiona = neuer_user("fiona")
    kai = neuer_user("kai")
    bea = neuer_user("bea")
    bob = neuer_user("bob")
    for f in (finn, fiona, bea, bob):
        db.add(models.Follows(follower_id=f.id, followee_id=olga.id))
    db.add(models.Follows(follower_id=olga.id, followee_id=kai.id))
    db.add(models.Block(blocker_id=olga.id, blocked_id=bea.id))
    db.add(models.Block(blocker_id=bob.id, blocked_id=olga.id))
    db.flush()

    def ranking_post(owner, eingestellt=HEUTE):
        p = neuer_post(owner)
        p.ranking_entered_on = eingestellt
        db.flush()
        return p

    def swipen(post_id, user, liked=True):
        """Ruft die Route auf wie eine frische Anfrage (Identity-Map geleert)."""
        db.expire_all()
        try:
            antwort = profile_ranking.swipe_profile_post(
                swipe=schemas.SwipeIn(post_id=post_id, direction=liked), db=db, current_user=user)
            return 201, antwort
        except HTTPException as e:
            return e.status_code, e.detail

    def likes_von(post):
        db.expire_all()
        return db.query(models.Post.ranking_like_count).filter(models.Post.id == post.id).scalar()

    def swipe_zeile(user, post):
        db.expire_all()
        return db.query(models.ProfileRankingSwipe).filter(
            models.ProfileRankingSwipe.voter_id == user.id,
            models.ProfileRankingSwipe.post_id == post.id).first()

    def anzahl_swipes(post):
        db.expire_all()
        return db.query(models.ProfileRankingSwipe).filter(
            models.ProfileRankingSwipe.post_id == post.id).count()

    TEXT_NICHT_IM_RANKING = "Post ist nicht im Ranking."
    TEXT_EIGENER = "Eigene Posts kannst du nicht bewerten."
    TEXT_KEIN_FOLLOWER = "Du musst dem Besitzer folgen, um zu bewerten."
    TEXT_BLOCK = "Du kannst diesen Post nicht bewerten."
    TEXT_ABGELAUFEN = "Die Bewertungszeit für diesen Post ist abgelaufen."
    TEXT_SCHON_GESWIPT = "Diesen Post hast du schon bewertet."

    # ---------------------------------------------------------------
    print("\n[11] Route /swipes ist eingebunden, Body wird geprueft")
    check("POST /profile_ranking/swipes existiert", ("/profile_ranking/swipes", "POST") in pfade)
    try:
        schemas.SwipeIn(post_id=0, direction=True)
        check("post_id=0 wird abgelehnt", False)
    except ValidationError:
        check("post_id=0 wird abgelehnt", True)

    # ---------------------------------------------------------------
    print("\n[12] 404: Post fehlt oder ist nicht im Ranking")
    code, text = swipen(hoechste_id + 1000, finn)
    check("Post existiert nicht -> 404", code == 404, f"({code} {text})")
    normal = neuer_post(olga)
    code, text = swipen(normal.id, finn)
    check("Post nicht im Ranking -> 404",
          code == 404 and text == TEXT_NICHT_IM_RANKING, f"({code} {text})")
    check("keine Swipe-Zeile", anzahl_swipes(normal) == 0)

    p1 = ranking_post(olga)

    # ---------------------------------------------------------------
    print("\n[13] 403: eigener Post")
    code, text = swipen(p1.id, olga)
    check("403 eigener Post", code == 403 and text == TEXT_EIGENER, f"({code} {text})")
    check("keine Swipe-Zeile", anzahl_swipes(p1) == 0)
    check("Likes bleiben 0", likes_von(p1) == 0)

    # ---------------------------------------------------------------
    print("\n[14] 403: kein Follower (Richtung zaehlt)")
    code, text = swipen(p1.id, kai)
    check("olga folgt kai, kai nicht olga -> 403",
          code == 403 and text == TEXT_KEIN_FOLLOWER, f"({code} {text})")
    code, text = swipen(p1.id, anna)
    check("ganz Fremde -> 403", code == 403 and text == TEXT_KEIN_FOLLOWER, f"({code} {text})")
    check("keine Swipe-Zeile", anzahl_swipes(p1) == 0)

    # ---------------------------------------------------------------
    print("\n[15] 403: Block in beide Richtungen (obwohl Follower)")
    code, text = swipen(p1.id, bea)
    check("Besitzerin hat Swiper blockiert -> 403",
          code == 403 and text == TEXT_BLOCK, f"({code} {text})")
    code, text = swipen(p1.id, bob)
    check("Swiper hat Besitzerin blockiert -> 403",
          code == 403 and text == TEXT_BLOCK, f"({code} {text})")
    check("keine Swipe-Zeile", anzahl_swipes(p1) == 0)
    check("Likes bleiben 0", likes_von(p1) == 0)

    # ---------------------------------------------------------------
    print(f"\n[16] Frist: {PROFILE_RANKING_DAYS} Kalendertage ab Einstellen")
    letzter_tag = ranking_post(olga, HEUTE - timedelta(days=PROFILE_RANKING_DAYS - 1))
    code, antwort = swipen(letzter_tag.id, finn)
    check(f"eingestellt vor {PROFILE_RANKING_DAYS - 1} Tagen (letzter Tag) -> 201",
          code == 201, f"({code} {antwort})")
    abgelaufen = ranking_post(olga, HEUTE - timedelta(days=PROFILE_RANKING_DAYS))
    code, text = swipen(abgelaufen.id, finn)
    check(f"eingestellt vor {PROFILE_RANKING_DAYS} Tagen -> 409",
          code == 409 and text == TEXT_ABGELAUFEN, f"({code} {text})")
    check("abgelaufen: keine Swipe-Zeile", anzahl_swipes(abgelaufen) == 0)
    check("abgelaufen: Likes eingefroren (0)", likes_von(abgelaufen) == 0)

    # ---------------------------------------------------------------
    print("\n[17] Reihenfolge der Pruefungen")
    code, text = swipen(normal.id, olga)
    check("eigener + nicht im Ranking -> 404", code == 404, f"({code} {text})")
    code, text = swipen(abgelaufen.id, kai)
    check("kein Follower + abgelaufen -> 403 (Follower)",
          code == 403 and text == TEXT_KEIN_FOLLOWER, f"({code} {text})")
    code, text = swipen(abgelaufen.id, bea)
    check("Block + abgelaufen -> 403 (Block)", code == 403 and text == TEXT_BLOCK, f"({code} {text})")

    # ---------------------------------------------------------------
    print("\n[18] Like zaehlt +1, Ueberspringen zaehlt nicht")
    xp_vorher = {u.id: (u.xp, u.streak_count) for u in (finn, fiona, olga)}
    code, antwort = swipen(p1.id, finn, liked=True)
    check("Like -> 201", code == 201, f"({code} {antwort})")
    check("Antwort {post_id, liked: True}",
          code == 201 and antwort == {"post_id": p1.id, "liked": True}, f"({antwort})")
    zeile = swipe_zeile(finn, p1)
    check("Swipe-Zeile liked=True", zeile is not None and zeile.liked is True)
    check("Likes = 1", likes_von(p1) == 1, f"({likes_von(p1)})")

    code, antwort = swipen(p1.id, fiona, liked=False)
    check("Ueberspringen -> 201", code == 201, f"({code} {antwort})")
    check("Antwort liked: False", code == 201 and antwort == {"post_id": p1.id, "liked": False})
    zeile = swipe_zeile(fiona, p1)
    check("Swipe-Zeile liked=False", zeile is not None and zeile.liked is False)
    check("Likes bleiben 1", likes_von(p1) == 1, f"({likes_von(p1)})")

    # ---------------------------------------------------------------
    print("\n[19] 409: schon geswipt, nichts aendert sich")
    code, text = swipen(p1.id, finn, liked=True)
    check("Like doppelt -> 409", code == 409 and text == TEXT_SCHON_GESWIPT, f"({code} {text})")
    check("Likes bleiben 1 (kein doppeltes +1)", likes_von(p1) == 1, f"({likes_von(p1)})")
    code, text = swipen(p1.id, fiona, liked=True)
    check("Ueberspringen -> danach Like -> 409 (keine Ruecknahme)",
          code == 409 and text == TEXT_SCHON_GESWIPT, f"({code} {text})")
    check("fionas Zeile bleibt liked=False", swipe_zeile(fiona, p1).liked is False)
    check("Likes bleiben 1", likes_von(p1) == 1, f"({likes_von(p1)})")
    code, text = swipen(p1.id, finn, liked=False)
    check("Like -> danach Ueberspringen -> 409", code == 409, f"({code} {text})")
    check("finns Zeile bleibt liked=True", swipe_zeile(finn, p1).liked is True)
    check("genau 2 Swipe-Zeilen", anzahl_swipes(p1) == 2, f"({anzahl_swipes(p1)})")

    # ---------------------------------------------------------------
    print("\n[20] Keine Punkte, kein XP, kein Streak")
    db.expire_all()
    for u in (finn, fiona, olga):
        db.refresh(u)
        check(f"{u.username.split('_')[1]}: XP + Streak unveraendert",
              (u.xp, u.streak_count) == xp_vorher[u.id], f"({(u.xp, u.streak_count)})")
    rs = db.query(models.RankingScores).filter(
        models.RankingScores.post_id.in_([p1.id, letzter_tag.id])).count()
    check("keine ranking_scores-Zeile", rs == 0, f"({rs})")

    # ---------------------------------------------------------------
    print("\n[21] Zaehler pro Post getrennt")
    p2 = ranking_post(olga, HEUTE - timedelta(days=2))
    code, _ = swipen(p2.id, fiona, liked=True)
    check("fiona liked p2 -> 201", code == 201)
    code, _ = swipen(p2.id, finn, liked=True)
    check("finn liked p2 -> 201", code == 201)
    check("p2 Likes = 2", likes_von(p2) == 2, f"({likes_von(p2)})")
    check("p1 Likes weiter 1", likes_von(p1) == 1, f"({likes_von(p1)})")
    check("letzter_tag Likes = 1 (aus [16])", likes_von(letzter_tag) == 1, f"({likes_von(letzter_tag)})")

    # ===============================================================
    # GET /profile_ranking/users/{user_id}/open
    # ===============================================================
    def offen(user_id, user):
        """Ruft /open auf. Liefert (200, can_swipe, [post_ids]) oder (Fehlercode, Text, None)."""
        db.expire_all()
        try:
            antwort = profile_ranking.open_profile_posts(user_id=user_id, db=db, current_user=user)
            # durch das Antwort-Schema schicken wie FastAPI (prueft owner/location usw.)
            geprueft = schemas.ProfileRankingOpenOut.model_validate(antwort)
            return 200, geprueft.can_swipe, [p.id for p in geprueft.posts]
        except HTTPException as e:
            return e.status_code, e.detail, None

    # Eigene Besitzerin mit frischen Posts, damit [11]-[21] nicht reinspielen.
    #   nora = Besitzerin; finn, fiona, bea, bob folgen nora; nora hat bea blockiert,
    #   bob hat nora blockiert; kai folgt nora nicht.
    nora = neuer_user("nora")
    for f in (finn, fiona, bea, bob):
        db.add(models.Follows(follower_id=f.id, followee_id=nora.id))
    db.add(models.Block(blocker_id=nora.id, blocked_id=bea.id))
    db.add(models.Block(blocker_id=bob.id, blocked_id=nora.id))
    db.flush()

    n_neu = ranking_post(nora, HEUTE)
    n_mitte = ranking_post(nora, HEUTE - timedelta(days=3))
    n_letzter = ranking_post(nora, HEUTE - timedelta(days=PROFILE_RANKING_DAYS - 1))
    n_abgelaufen = ranking_post(nora, HEUTE - timedelta(days=PROFILE_RANKING_DAYS))
    n_normal = neuer_post(nora)  # nie im Ranking

    # ---------------------------------------------------------------
    print("\n[22] Route /open ist eingebunden")
    check("GET /profile_ranking/users/{user_id}/open existiert",
          ("/profile_ranking/users/{user_id}/open", "GET") in pfade)

    # ---------------------------------------------------------------
    print("\n[23] Follower sieht aktive Posts, aelteste zuerst")
    code, darf, ids = offen(nora.id, finn)
    check("200 + can_swipe", code == 200 and darf is True, f"({code} {darf})")
    check("genau die drei aktiven, aelteste zuerst",
          ids == [n_letzter.id, n_mitte.id, n_neu.id], f"({ids})")
    check("abgelaufener Post fehlt", ids is not None and n_abgelaufen.id not in ids)
    check("Post ausserhalb des Rankings fehlt", ids is not None and n_normal.id not in ids)

    # ---------------------------------------------------------------
    print("\n[24] Geswipte Posts verschwinden (Like und Ueberspringen)")
    swipen(n_mitte.id, finn, liked=True)
    swipen(n_neu.id, finn, liked=False)
    code, darf, ids = offen(nora.id, finn)
    check("nur noch der letzte Tag offen", ids == [n_letzter.id], f"({ids})")
    code, darf, ids = offen(nora.id, fiona)
    check("fiona sieht weiter alle drei", ids == [n_letzter.id, n_mitte.id, n_neu.id], f"({ids})")
    swipen(n_letzter.id, finn, liked=True)
    code, darf, ids = offen(nora.id, finn)
    check("alles geswipt -> can_swipe bleibt True, Liste leer",
          code == 200 and darf is True and ids == [], f"({code} {darf} {ids})")

    # ---------------------------------------------------------------
    print("\n[25] Selbst gemeldeter Post ist ausgeblendet")
    db.add(models.Report(reporter_id=fiona.id, reported_user_id=nora.id, target_type="post",
                         target_id=n_mitte.id, reason="spam"))
    db.flush()
    code, darf, ids = offen(nora.id, fiona)
    check("fiona sieht n_mitte nicht mehr", ids == [n_letzter.id, n_neu.id], f"({ids})")
    andere = neuer_user("otto")
    db.add(models.Follows(follower_id=andere.id, followee_id=nora.id))
    db.flush()
    code, darf, ids = offen(nora.id, andere)
    check("fremde Meldung blendet nicht aus", ids == [n_letzter.id, n_mitte.id, n_neu.id], f"({ids})")

    # ---------------------------------------------------------------
    print("\n[26] can_swipe=false: eigenes Profil, kein Follower, Block, Sperre")
    code, darf, ids = offen(nora.id, nora)
    check("eigenes Profil", code == 200 and darf is False and ids == [], f"({code} {darf} {ids})")
    code, darf, ids = offen(nora.id, kai)
    check("kein Follower", code == 200 and darf is False and ids == [], f"({code} {darf} {ids})")
    code, darf, ids = offen(nora.id, bea)
    check("Besitzerin hat mich blockiert", code == 200 and darf is False and ids == [],
          f"({code} {darf} {ids})")
    code, darf, ids = offen(nora.id, bob)
    check("ich habe Besitzerin blockiert", code == 200 and darf is False and ids == [],
          f"({code} {darf} {ids})")

    db.query(models.User).filter(models.User.id == nora.id).update(
        {models.User.banned_until: JETZT + timedelta(days=1)}, synchronize_session=False)
    db.flush()
    code, darf, ids = offen(nora.id, fiona)
    check("Besitzerin gesperrt", code == 200 and darf is False and ids == [], f"({code} {darf} {ids})")
    db.query(models.User).filter(models.User.id == nora.id).update(
        {models.User.banned_until: JETZT - timedelta(minutes=1)}, synchronize_session=False)
    db.flush()
    code, darf, ids = offen(nora.id, fiona)
    check("Sperre abgelaufen -> wieder offen", code == 200 and darf is True and len(ids) == 2,
          f"({code} {darf} {ids})")

    # ---------------------------------------------------------------
    print("\n[27] 404: User existiert nicht")
    hoechste_uid = db.query(models.User.id).order_by(models.User.id.desc()).limit(1).scalar()
    code, text, _ = offen(hoechste_uid + 1000, finn)
    check("404", code == 404, f"({code} {text})")

    # ===============================================================
    # GET /profile_ranking/users/{user_id}/posts  (Ranking-Tab)
    # ===============================================================
    def ansehen(user_id, user, limit=50, skip=0):
        """Ruft den Ranking-Tab auf. Liefert (200, [ProfileRankingPostOut]) oder (Fehlercode, Text)."""
        db.expire_all()
        try:
            antwort = profile_ranking.profile_ranking_posts(
                user_id=user_id, limit=limit, skip=skip, db=db, current_user=user)
            return 200, [schemas.ProfileRankingPostOut.model_validate(a) for a in antwort]
        except HTTPException as e:
            return e.status_code, e.detail

    def ids_von(liste):
        return [a.post.id for a in liste] if isinstance(liste, list) else liste

    # rita = Besitzerin. Like-Zahlen direkt gesetzt, damit die Sortierung klar ist.
    #   bea: rita hat bea blockiert; bob: bob blockiert rita; kai folgt rita nicht.
    rita = neuer_user("rita")
    db.add(models.Block(blocker_id=rita.id, blocked_id=bea.id))
    db.add(models.Block(blocker_id=bob.id, blocked_id=rita.id))
    db.flush()

    def mit_likes(eingestellt, likes):
        p = ranking_post(rita, eingestellt)
        p.ranking_like_count = likes
        db.flush()
        return p

    r_a = mit_likes(HEUTE - timedelta(days=10), 5)                     # abgelaufen
    r_b = mit_likes(HEUTE - timedelta(days=2), 5)                      # gleich viele Likes, neuer
    r_c = mit_likes(HEUTE, 9)
    r_d = mit_likes(HEUTE - timedelta(days=1), 0)
    r_f = mit_likes(HEUTE - timedelta(days=3), 2)                      # von finn gemeldet
    r_g = mit_likes(HEUTE - timedelta(days=4), 1)
    r_h = mit_likes(HEUTE - timedelta(days=4), 1)                      # gleich mit r_g -> id entscheidet
    r_six = mit_likes(HEUTE - timedelta(days=PROFILE_RANKING_DAYS - 1), 0)  # letzter aktiver Tag
    r_grenze = mit_likes(HEUTE - timedelta(days=PROFILE_RANKING_DAYS), 0)   # erster abgelaufener Tag
    r_normal = neuer_post(rita)                                         # nie im Ranking

    db.add(models.Report(reporter_id=finn.id, reported_user_id=rita.id, target_type="post",
                         target_id=r_f.id, reason="spam"))
    db.add(models.ProfileRankingSwipe(voter_id=finn.id, post_id=r_c.id, liked=True))
    db.add(models.ProfileRankingSwipe(voter_id=finn.id, post_id=r_d.id, liked=False))
    db.flush()

    ERWARTET_FINN = [r_c.id, r_b.id, r_a.id, r_h.id, r_g.id, r_d.id, r_six.id, r_grenze.id]
    ERWARTET_ALLE = [r_c.id, r_b.id, r_a.id, r_f.id, r_h.id, r_g.id, r_d.id, r_six.id, r_grenze.id]

    # ---------------------------------------------------------------
    print("\n[28] Route /posts ist eingebunden")
    check("GET /profile_ranking/users/{user_id}/posts existiert",
          ("/profile_ranking/users/{user_id}/posts", "GET") in pfade)

    # ---------------------------------------------------------------
    print("\n[29] Sortierung: Likes, dann Datum, dann id; gemeldete ausgeblendet")
    code, liste = ansehen(rita.id, finn)
    check("200", code == 200, f"({code})")
    check("Reihenfolge stimmt, r_f (gemeldet) fehlt", ids_von(liste) == ERWARTET_FINN,
          f"({ids_von(liste)})")
    check("Post ausserhalb des Rankings fehlt", r_normal.id not in ids_von(liste))
    code, liste_fiona = ansehen(rita.id, fiona)
    check("fiona (nichts gemeldet) sieht auch r_f", ids_von(liste_fiona) == ERWARTET_ALLE,
          f"({ids_von(liste_fiona)})")

    # ---------------------------------------------------------------
    print("\n[30] Felder: Like-Zahl, Datum, is_active, my_swipe")
    nach_id = {a.post.id: a for a in liste}
    check("Like-Zahlen", [nach_id[i].ranking_like_count for i in ERWARTET_FINN] == [9, 5, 5, 1, 1, 0, 0, 0])
    check("ranking_entered_on", nach_id[r_b.id].ranking_entered_on == HEUTE - timedelta(days=2))
    check("is_active: heute -> True", nach_id[r_c.id].is_active is True)
    check(f"is_active: vor {PROFILE_RANKING_DAYS - 1} Tagen -> True", nach_id[r_six.id].is_active is True)
    check(f"is_active: vor {PROFILE_RANKING_DAYS} Tagen -> False", nach_id[r_grenze.id].is_active is False)
    check("is_active: vor 10 Tagen -> False", nach_id[r_a.id].is_active is False)
    check("my_swipe: geliked -> True", nach_id[r_c.id].my_swipe is True)
    check("my_swipe: uebersprungen -> False", nach_id[r_d.id].my_swipe is False)
    check("my_swipe: nicht geswipt -> None", nach_id[r_b.id].my_swipe is None)
    check("my_swipe gilt nur fuer mich (fiona: alles None)",
          all(a.my_swipe is None for a in liste_fiona))
    check("Besitzer mitgeladen", nach_id[r_c.id].post.owner.id == rita.id)

    # ---------------------------------------------------------------
    print("\n[31] Seiten: skip/limit liefern alles genau einmal")
    seiten = []
    for s in (0, 3, 6, 9):
        code, teil = ansehen(rita.id, finn, limit=3, skip=s)
        seiten += ids_von(teil)
    check("drei Seiten = ganze Liste, nichts doppelt", seiten == ERWARTET_FINN, f"({seiten})")
    code, teil = ansehen(rita.id, finn, limit=3, skip=6)
    check("letzte Seite hat 2", len(teil) == 2, f"({len(teil)})")

    # ---------------------------------------------------------------
    print("\n[32] Wer darf ansehen")
    code, liste = ansehen(rita.id, kai)
    check("Nicht-Follower -> 200 mit Liste", code == 200 and ids_von(liste) == ERWARTET_ALLE,
          f"({code} {ids_von(liste)})")
    code, liste = ansehen(rita.id, rita)
    check("eigenes Profil -> 200 mit Liste", code == 200 and ids_von(liste) == ERWARTET_ALLE,
          f"({code} {ids_von(liste)})")
    code, text = ansehen(rita.id, bea)
    check("Besitzerin hat mich blockiert -> 403", code == 403, f"({code} {text})")
    code, text = ansehen(rita.id, bob)
    check("ich habe Besitzerin blockiert -> 403", code == 403, f"({code} {text})")
    code, text = ansehen(hoechste_uid + 1000, finn)
    check("User existiert nicht -> 404", code == 404, f"({code} {text})")
    code, liste = ansehen(olga.id, kai)
    check("kai (folgt olga nicht) sieht olgas Liste", code == 200 and len(liste) > 0)
    code, liste = ansehen(kai.id, finn)
    check("Profil ohne Ranking-Posts -> leere Liste", code == 200 and liste == [], f"({code} {liste})")

    db.query(models.User).filter(models.User.id == rita.id).update(
        {models.User.banned_until: JETZT + timedelta(days=1)}, synchronize_session=False)
    db.flush()
    code, liste = ansehen(rita.id, fiona)
    check("Besitzerin gesperrt -> Tab bleibt sichtbar", code == 200 and ids_von(liste) == ERWARTET_ALLE,
          f"({code} {ids_von(liste)})")
    db.query(models.User).filter(models.User.id == rita.id).update(
        {models.User.banned_until: None}, synchronize_session=False)
    db.flush()

    # ===============================================================
    # Profilzahl ranking_likes_total in GET /users/ und GET /users/{id}
    # ===============================================================
    from sqlalchemy import event  # noqa: E402
    from app.routers import user as user_router  # noqa: E402

    anweisungen = []

    def _zaehlen(_conn, _cursor, stmt, *_rest):
        anweisungen.append(stmt)

    event.listen(engine, "before_cursor_execute", _zaehlen)

    def _ich_laden(ich):
        """Frische Anfrage simulieren: alles vergessen, den eingeloggten User (wie oauth2) vorab laden."""
        db.expire_all()
        ich.id, ich.location  # noqa: B018 - nur laden, damit die Route allein gezaehlt wird
        anweisungen.clear()

    def profil(user_id, ich):
        """GET /users/{id}. Liefert (200, GetUserOut, Anzahl Anweisungen) oder (Fehlercode, Text, None)."""
        _ich_laden(ich)
        try:
            antwort = user_router.get_user(id=user_id, current_user=ich, db=db)
            n = len(anweisungen)
            return 200, schemas.GetUserOut.model_validate(antwort), n
        except HTTPException as e:
            return e.status_code, e.detail, None

    def eigenes(ich):
        """GET /users/. Liefert (GetUserOut, Anzahl Anweisungen)."""
        _ich_laden(ich)
        antwort = user_router.get_current_user(current_user=ich, db=db)
        n = len(anweisungen)
        return schemas.GetUserOut.model_validate(antwort), n

    # rita: 5+5+9+0+2+1+1+0+0 aus [29], r_normal ist nicht im Ranking
    RITA_SUMME = 23

    # ---------------------------------------------------------------
    print("\n[33] Fremdes Profil: Summe, Follower, is_followed in EINER Anweisung")
    code, out, n = profil(rita.id, finn)
    check("200", code == 200, f"({code})")
    check(f"ranking_likes_total = {RITA_SUMME}", code == 200 and out.ranking_likes_total == RITA_SUMME,
          f"({out.ranking_likes_total if code == 200 else out})")
    check("follower_count = 0", code == 200 and out.follower_count == 0)
    check("is_followed = False", code == 200 and out.is_followed is False)
    check("1 Anweisung", n == 1, f"({n})")

    # ---------------------------------------------------------------
    print("\n[34] Nur Ranking-Likes zaehlen, normale Likes nicht")
    r_normal.vote_count = 50
    db.flush()
    code, out, _ = profil(rita.id, finn)
    check("vote_count auf normalem Post aendert nichts", out.ranking_likes_total == RITA_SUMME,
          f"({out.ranking_likes_total})")

    # ---------------------------------------------------------------
    print("\n[35] Follower und is_followed")
    db.add(models.Follows(follower_id=finn.id, followee_id=rita.id))
    db.add(models.Follows(follower_id=fiona.id, followee_id=rita.id))
    db.flush()
    code, out, n = profil(rita.id, finn)
    check("follower_count = 2", out.follower_count == 2, f"({out.follower_count})")
    check("finn folgt -> is_followed = True", out.is_followed is True)
    check("weiter 1 Anweisung", n == 1, f"({n})")
    code, out, _ = profil(rita.id, kai)
    check("kai folgt nicht -> is_followed = False", out.is_followed is False)
    check("kai sieht dieselbe Summe", out.ranking_likes_total == RITA_SUMME, f"({out.ranking_likes_total})")

    # ---------------------------------------------------------------
    print("\n[36] Eigenes Profil GET /users/")
    out, n = eigenes(rita)
    check(f"ranking_likes_total = {RITA_SUMME}", out.ranking_likes_total == RITA_SUMME,
          f"({out.ranking_likes_total})")
    check("follower_count = 2", out.follower_count == 2, f"({out.follower_count})")
    check("1 Anweisung", n == 1, f"({n})")
    check("eigene Felder weiter da (email, role)", out.email == rita.email and out.role is not None)

    # ---------------------------------------------------------------
    print("\n[37] Ohne Posts: 0 statt None")
    leer = neuer_user("leer")
    code, out, _ = profil(leer.id, finn)
    check("fremd: ranking_likes_total = 0", out.ranking_likes_total == 0, f"({out.ranking_likes_total})")
    check("fremd: follower_count = 0", out.follower_count == 0, f"({out.follower_count})")
    out, _ = eigenes(leer)
    check("eigen: ranking_likes_total = 0", out.ranking_likes_total == 0, f"({out.ranking_likes_total})")
    check("eigen: follower_count = 0", out.follower_count == 0, f"({out.follower_count})")

    # ---------------------------------------------------------------
    print("\n[38] Swipes wirken auf die Summe")
    code, _ = swipen(r_g.id, fiona, liked=True)
    check("fiona liked r_g -> 201", code == 201, f"({code})")
    code, out, _ = profil(rita.id, finn)
    check(f"Summe = {RITA_SUMME + 1}", out.ranking_likes_total == RITA_SUMME + 1, f"({out.ranking_likes_total})")
    code, _ = swipen(r_b.id, finn, liked=False)
    check("finn ueberspringt r_b -> 201", code == 201, f"({code})")
    out, _ = eigenes(rita)
    check(f"Summe bleibt {RITA_SUMME + 1}", out.ranking_likes_total == RITA_SUMME + 1,
          f"({out.ranking_likes_total})")

    # ---------------------------------------------------------------
    print("\n[39] Summe gehoert nur dem Besitzer")
    db.expire_all()
    olga_erwartet = sum(p.ranking_like_count for p in db.query(models.Post).filter(models.Post.owner_id == olga.id))
    code, out, _ = profil(olga.id, finn)
    check("olga: Summe ihrer eigenen Posts", out.ranking_likes_total == olga_erwartet,
          f"({out.ranking_likes_total} statt {olga_erwartet})")
    check("olga hat ueberhaupt Likes (sonst waere der Test leer)", olga_erwartet > 0, f"({olga_erwartet})")

    # ---------------------------------------------------------------
    print("\n[40] Unbekannter User und Suche")
    code, text, _ = profil(hoechste_uid + 1000, finn)
    check("404", code == 404, f"({code} {text})")
    treffer = user_router.search_for_user(search=rita.username, current_user=finn, db=db)
    such_out = [schemas.GetUserOut.model_validate(t) for t in treffer]
    check("Suche findet rita", any(t.id == rita.id for t in such_out))
    check("Suche liefert keine Summe (None)", all(t.ranking_likes_total is None for t in such_out))

    event.remove(engine, "before_cursor_execute", _zaehlen)

    # ===============================================================
    # Schritt 7: Account-Loeschung, Post-Loeschung, Cleanup
    # ===============================================================
    # maja = Besitzerin, lisa = Likerin (wird geloescht), tom = zweiter Liker
    maja = neuer_user("maja")
    lisa = neuer_user("lisa")
    tom = neuer_user("tom")
    for f in (lisa, tom):
        db.add(models.Follows(follower_id=f.id, followee_id=maja.id))
    db.flush()
    m_aktiv = ranking_post(maja)
    m_alt = ranking_post(maja, HEUTE - timedelta(days=PROFILE_RANKING_DAYS + 3))  # abgelaufen
    m_skip = ranking_post(maja)

    swipen(m_aktiv.id, lisa, liked=True)
    swipen(m_aktiv.id, tom, liked=True)
    swipen(m_skip.id, lisa, liked=False)
    # Like auf dem abgelaufenen Post stammt aus seiner aktiven Zeit -> direkt einfuegen
    db.add(models.ProfileRankingSwipe(voter_id=lisa.id, post_id=m_alt.id, liked=True))
    m_alt.ranking_like_count = 1
    db.flush()

    # ---------------------------------------------------------------
    print("\n[41] Account-Loeschung zieht Likes ab")
    check("Vorher: m_aktiv 2, m_alt 1, m_skip 0",
          (likes_von(m_aktiv), likes_von(m_alt), likes_von(m_skip)) == (2, 1, 0),
          f"({likes_von(m_aktiv)}, {likes_von(m_alt)}, {likes_von(m_skip)})")
    rita_vorher = profil(rita.id, finn)[1].ranking_likes_total
    lisa_id = lisa.id
    user_router._delete_user_data(lisa, db)
    db.flush()
    check("m_aktiv: 2 -> 1 (toms Like bleibt)", likes_von(m_aktiv) == 1, f"({likes_von(m_aktiv)})")
    check("m_alt: 1 -> 0 (auch abgelaufene Posts)", likes_von(m_alt) == 0, f"({likes_von(m_alt)})")
    check("m_skip bleibt 0 (Ueberspringen zieht nichts ab)", likes_von(m_skip) == 0, f"({likes_von(m_skip)})")
    check("lisas Swipe-Zeilen per CASCADE weg",
          db.query(models.ProfileRankingSwipe).filter(models.ProfileRankingSwipe.voter_id == lisa_id).count() == 0)
    check("Zaehler = Anzahl liked-Zeilen (wie beim Restore)",
          all(likes_von(p) == db.query(models.ProfileRankingSwipe).filter(
              models.ProfileRankingSwipe.post_id == p.id, models.ProfileRankingSwipe.liked.is_(True)).count()
              for p in (m_aktiv, m_alt, m_skip)))
    code, out, _ = profil(maja.id, tom)
    check("majas Summe = 1", out.ranking_likes_total == 1, f"({out.ranking_likes_total})")
    check("fremdes Profil (rita) unveraendert",
          profil(rita.id, finn)[1].ranking_likes_total == rita_vorher)

    # ---------------------------------------------------------------
    print("\n[42] Post-Loeschung nimmt seine Likes aus der Summe")
    db.delete(db.get(models.Post, m_aktiv.id))
    db.flush()
    code, out, _ = profil(maja.id, tom)
    check("majas Summe = 0", out.ranking_likes_total == 0, f"({out.ranking_likes_total})")

    # ---------------------------------------------------------------
    print("\n[43] DELETE /cleanup/profile_ranking_skips")
    from app.config import settings  # noqa: E402
    from app.ranking_config import PROFILE_RANKING_SKIP_RETENTION_DAYS as FRIST  # noqa: E402
    from app.routers import cleanup  # noqa: E402

    c_post = ranking_post(maja)
    alt_skip, frisch_skip, alt_like = neuer_user("c1"), neuer_user("c2"), neuer_user("c3")
    for u, liked, tage in ((alt_skip, False, FRIST + 1), (frisch_skip, False, FRIST - 1), (alt_like, True, FRIST + 30)):
        db.add(models.ProfileRankingSwipe(voter_id=u.id, post_id=c_post.id, liked=liked,
                                          created_at=JETZT - timedelta(days=tage)))
    c_post.ranking_like_count = 1
    db.flush()

    try:
        cleanup.delete_profile_ranking_skips(db=db, x_cleanup_secret="falsch")
        check("falsches Secret -> 401", False, "kein Fehler")
    except HTTPException as e:
        check("falsches Secret -> 401", e.status_code == 401, f"({e.status_code})")
    check("  ... nichts geloescht", anzahl_swipes(c_post) == 3, f"({anzahl_swipes(c_post)})")

    out = cleanup.delete_profile_ranking_skips(db=db, x_cleanup_secret=settings.story_cleanup_secret)
    check("Antwort {deleted: n}, n >= 1", set(out) == {"deleted"} and out["deleted"] >= 1, f"({out})")
    check(f"Ueberspringen > {FRIST} Tage weg", swipe_zeile(alt_skip, c_post) is None)
    check(f"Ueberspringen < {FRIST} Tage bleibt", swipe_zeile(frisch_skip, c_post) is not None)
    check("alter Like bleibt", swipe_zeile(alt_like, c_post) is not None)
    check("Zaehler unveraendert (1)", likes_von(c_post) == 1, f"({likes_von(c_post)})")
    out = cleanup.delete_profile_ranking_skips(db=db, x_cleanup_secret=settings.story_cleanup_secret)
    check("zweiter Lauf -> deleted 0", out == {"deleted": 0}, f"({out})")

finally:
    db.close()
    outer.rollback()
    conn.close()

print(f"\n{'ALLES GRUEN' if fehler == 0 else f'{fehler} FEHLER'}  (zurueckgerollt, nichts gespeichert)")
sys.exit(1 if fehler else 0)
