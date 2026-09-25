"""Vergleich alter Feed-Weg gegen Rerank-Weg (Lytir Block 5c).

Mit dem Platzhalter-Ranker (alle Scores 0.0) MUSS der Rerank-Weg exakt
dieselben Seiten liefern wie der alte SQL-Weg. Tut er das nicht, stimmt die
Mechanik (150 holen, Seite ausschneiden, Cache schreiben) nicht — und mit dem
echten Modell faellt so ein Fehler nie mehr auf, weil sich die Reihenfolge
dann ohnehin aendert.

Der Schalter wird NUR in diesem Prozess umgelegt (feed.LYTIR_RERANK_ENABLED),
ranking_config.py bleibt unangetastet. Das geht, weil seite_holen() den Namen
bei jedem Aufruf neu im Modul `feed` nachschlaegt.

  V1  Seiten alt == neu (IDs, votes, is_mine, is_liked), mehrere limit/skip
  V2  Rerank-Weg braucht weiterhin genau 1 SQL-Anweisung
  V3  Cache: nach Seite 1 steht die KOMPLETTE Reihenfolge in Redis, mit TTL
  V4  Suche umgeht Rerank UND Cache
  V5  Lokal-Feed (mit dem ersten User, der einen Ort hat)
  V6  Cache-Treffer: Seite 2 aus der gemerkten Reihenfolge, 1 Anweisung,
      und der TTL wird dabei NICHT verlaengert
  V7  Fenstergrenze: mit FEED_CANDIDATE_LIMIT = 5 muss nachgeladen werden
  V8  Eine ID im Cache, zu der es keinen Post (mehr) gibt, faellt raus
  V9  Redis kaputt: der Feed liefert trotzdem die richtigen Seiten
  V10 Echtes Modell (Lytir 6b): ranker.GEWICHTE zeigt NUR in diesem Prozess
      auf training/data/lytir.json. Dieselben Posts, nach Score sortiert,
      weiterhin 1 Anweisung, plus Zeitmessung fuer 150 Kandidaten

V1-V9 setzen voraus, dass KEIN Modell geladen ist (app/lytir/lytir.json
fehlt) — nur dann ist der Rerank-Weg mit dem alten Weg vergleichbar.

Schreibt nur die eigenen Cache-Keys in Redis und loescht sie am Ende wieder.
Die DB wird nur gelesen.

Aufruf:  .\\venv\\Scripts\\python.exe scripts\\rerank_vergleich.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import redis  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app import feed, models  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.lytir import ranker  # noqa: E402
from app.ranking_config import FEED_CANDIDATE_LIMIT, FEED_ORDER_CACHE_TTL  # noqa: E402
from app.redis_client import redis_client  # noqa: E402
from app.routers.post import get_posts  # noqa: E402

if not hasattr(feed, "LYTIR_RERANK_ENABLED"):
    print("feed.py kennt LYTIR_RERANK_ENABLED noch nicht — erst 5c eintippen.")
    sys.exit(1)

if ranker._modell_laden() is not None:
    print(f"{ranker.GEWICHTE} existiert — V1-V9 brauchen den Zustand OHNE Modell.")
    print("Datei kurz wegschieben (V10 benutzt ohnehin training/data/lytir.json).")
    sys.exit(1)

SIM_GEWICHTE = ROOT / "training" / "data" / "lytir.json"

# Der Wert, der im Code steht. Nach jedem Abruf wird darauf zurueckgesetzt.
SCHALTER_IM_CODE = feed.LYTIR_RERANK_ENABLED

anweisungen = 0


@event.listens_for(engine, "after_cursor_execute")
def _zaehlen(conn, cursor, statement, parameters, context, executemany):
    global anweisungen
    anweisungen += 1


fehler: list[str] = []


def pruefen(name: str, ok: bool, detail: str = "") -> None:
    marke = "OK    " if ok else "FEHLER"
    print(f"  {marke}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        fehler.append(name)


def abruf(db, user, *, rerank: bool, **params):
    """get_posts() mit umgelegtem Schalter.

    Gibt (kurzform, anzahl_anweisungen) zurueck. Kurzform = alles, was die App
    von einem Post zu sehen bekommt, ausser dem Post-Inhalt selbst.
    """
    global anweisungen
    feed.LYTIR_RERANK_ENABLED = rerank
    anweisungen = 0
    try:
        seite = get_posts(db=db, current_user=user, **params)
    finally:
        feed.LYTIR_RERANK_ENABLED = SCHALTER_IM_CODE
    kurz = [(p["post"].id, p["votes"], p["is_mine"], p["is_liked"]) for p in seite]
    return kurz, anweisungen


def ids(kurz) -> list[int]:
    return [post_id for post_id, *_ in kurz]


# ---------------------------------------------------------------------------
db = SessionLocal()

user = db.query(models.User).order_by(models.User.id).first()
if user is None:
    print("Keine User in der DB — nichts zu vergleichen.")
    sys.exit(1)

try:
    redis_client.ping()
    redis_da = True
except redis.RedisError as e:
    print(f"Redis nicht erreichbar ({e}) — V3/V4-Cache-Teil werden uebersprungen.")
    redis_da = False

print(f"Testuser: id={user.id}, location_id={user.location_id}")
print(f"Schalter im Code: LYTIR_RERANK_ENABLED = {SCHALTER_IM_CODE}")

benutzte_keys = {feed._feed_cache_key(user.id, False)}

# Die volle alte Reihenfolge — Referenz fuer V1-Plausibilitaet und V3.
alle_alt, _ = abruf(db, user, rerank=False, limit=FEED_CANDIDATE_LIMIT, skip=0)
print(f"Kandidaten im Fenster: {len(alle_alt)}")

# ---------------------------------------------------------------------------
print("\nV1) Seiten: alter Weg == Rerank-Weg")
if not alle_alt:
    pruefen("Feed ist nicht leer", False, "ohne Posts sagt ein Vergleich nichts")
for limit, skip in [(5, 0), (5, 5), (5, 10), (5, 15), (10, 0), (10, 10)]:
    alt, _ = abruf(db, user, rerank=False, limit=limit, skip=skip)
    neu, _ = abruf(db, user, rerank=True, limit=limit, skip=skip)
    detail = f"{len(alt)} Posts"
    if alt != neu:
        detail = f"alt {ids(alt)} / neu {ids(neu)}"
    pruefen(f"limit={limit:2} skip={skip:2}", alt == neu, detail)

# ---------------------------------------------------------------------------
print("\nV2) Rundwege im Rerank-Weg")
_, n = abruf(db, user, rerank=True, limit=10, skip=0)
pruefen("genau 1 SQL-Anweisung", n == 1, f"{n} gezaehlt")

# ---------------------------------------------------------------------------
print("\nV3) Reihenfolge-Cache in Redis")
key = feed._feed_cache_key(user.id, False)   # privater Helfer — im Test erlaubt
if not redis_da:
    print("  UEBERSPRUNGEN  Redis nicht erreichbar")
else:
    redis_client.delete(key)
    abruf(db, user, rerank=True, limit=5, skip=0)
    roh = redis_client.get(key)
    ttl = redis_client.ttl(key)
    gemerkt = json.loads(roh) if roh is not None else None

    pruefen("Key wurde geschrieben", gemerkt is not None, key)
    pruefen("komplette Reihenfolge, nicht nur die Seite",
            gemerkt == ids(alle_alt),
            f"{len(gemerkt or [])} IDs gemerkt, {len(alle_alt)} erwartet")
    pruefen(f"TTL gesetzt (0 < ttl <= {FEED_ORDER_CACHE_TTL})",
            0 < ttl <= FEED_ORDER_CACHE_TTL, f"ttl={ttl}")

# ---------------------------------------------------------------------------
print("\nV4) Suche umgeht Rerank und Cache")
neuester = db.query(models.Post).order_by(models.Post.id.desc()).first()
suchwort = (neuester.title or "")[:3] if neuester else ""
if not suchwort:
    print("  UEBERSPRUNGEN  kein Post mit Titel zum Suchen")
else:
    if redis_da:
        redis_client.delete(key)
    alt, _ = abruf(db, user, rerank=False, limit=10, skip=0, search=suchwort)
    neu, _ = abruf(db, user, rerank=True, limit=10, skip=0, search=suchwort)
    pruefen(f"Suche '{suchwort}': alt == neu", alt == neu, f"{len(alt)} Treffer")
    if redis_da:
        pruefen("Suche schreibt keinen Cache", redis_client.get(key) is None)

# ---------------------------------------------------------------------------
print("\nV5) Lokal-Feed")
lokal_user = db.query(models.User).filter(
    models.User.location_id.isnot(None)
).order_by(models.User.id).first()
if lokal_user is None:
    print("  UNGETESTET  kein User mit Ort in der DB")
else:
    benutzte_keys.add(feed._feed_cache_key(lokal_user.id, True))
    print(f"  (mit User id={lokal_user.id}, location_id={lokal_user.location_id})")
    for limit, skip in [(5, 0), (5, 5)]:
        alt, _ = abruf(db, lokal_user, rerank=False, limit=limit, skip=skip, local=True)
        neu, _ = abruf(db, lokal_user, rerank=True, limit=limit, skip=skip, local=True)
        detail = f"{len(alt)} Posts"
        if alt != neu:
            detail = f"alt {ids(alt)} / neu {ids(neu)}"
        pruefen(f"local limit={limit} skip={skip}", alt == neu, detail)

# ---------------------------------------------------------------------------
print("\nV6) Cache-Treffer: Seite 2 kommt aus der gemerkten Reihenfolge")
if not redis_da:
    print("  UEBERSPRUNGEN  Redis nicht erreichbar")
else:
    redis_client.delete(key)

    # Seite 1, kalter Cache: holt das Fenster UND hat die Posts damit schon.
    kalt, n_kalt = abruf(db, user, rerank=True, limit=5, skip=0)
    pruefen("Seite 1 kalt: 1 Anweisung", n_kalt == 1, f"{n_kalt} gezaehlt")

    # Kuenstlich runtersetzen: wird der TTL neu gesetzt, steht gleich wieder 300.
    redis_client.expire(key, 100)

    # Seite 2, warmer Cache: nur noch WHERE id IN (...).
    alt2, _ = abruf(db, user, rerank=False, limit=5, skip=5)
    warm, n_warm = abruf(db, user, rerank=True, limit=5, skip=5)
    pruefen("Seite 2 warm == alter Weg", alt2 == warm,
            f"{len(alt2)} Posts" if alt2 == warm else f"alt {ids(alt2)} / neu {ids(warm)}")
    pruefen("Seite 2 warm: 1 Anweisung", n_warm == 1, f"{n_warm} gezaehlt")

    ttl = redis_client.ttl(key)
    pruefen("TTL nicht verlaengert (Reihenfolge friert nicht ein)",
            0 < ttl <= 100, f"ttl={ttl}, erwartet <= 100")

# ---------------------------------------------------------------------------
print("\nV7) Fenstergrenze: FEED_CANDIDATE_LIMIT = 5, nur in diesem Prozess")
if not redis_da:
    print("  UEBERSPRUNGEN  Redis nicht erreichbar")
elif len(alle_alt) < 6:
    print(f"  UEBERSPRUNGEN  nur {len(alle_alt)} Posts — zu wenig fuer mehrere Fenster")
else:
    limit_im_code = feed.FEED_CANDIDATE_LIMIT
    feed.FEED_CANDIDATE_LIMIT = 5
    try:
        redis_client.delete(key)
        gleich = True
        for skip in range(0, len(alle_alt) + 3, 3):
            alt, _ = abruf(db, user, rerank=False, limit=3, skip=skip)
            neu, _ = abruf(db, user, rerank=True, limit=3, skip=skip)
            if alt != neu:
                gleich = False
                print(f"         skip={skip}: alt {ids(alt)} / neu {ids(neu)}")
        pruefen("jede Seite gleich, obwohl in 5er-Fenstern nachgeladen", gleich)

        gemerkt = json.loads(redis_client.get(key) or "[]")
        pruefen("Cache haelt am Ende die komplette Reihenfolge",
                gemerkt == ids(alle_alt),
                f"{len(gemerkt)} IDs gemerkt, {len(alle_alt)} erwartet")
        pruefen("keine Dubletten ueber Fenstergrenzen hinweg",
                len(set(gemerkt)) == len(gemerkt))
    finally:
        feed.FEED_CANDIDATE_LIMIT = limit_im_code

# ---------------------------------------------------------------------------
print("\nV8) Tote ID im Cache (Post geloescht oder gemeldet)")
if not redis_da:
    print("  UEBERSPRUNGEN  Redis nicht erreichbar")
else:
    echte = ids(alle_alt)
    tote_id = max(echte) + 999999
    redis_client.set(key, json.dumps([tote_id] + echte), ex=FEED_ORDER_CACHE_TTL)

    seite, n = abruf(db, user, rerank=True, limit=3, skip=0)
    pruefen("tote ID faellt raus, Rest stimmt", ids(seite) == echte[:2],
            f"bekommen {ids(seite)}, erwartet {echte[:2]}")
    pruefen("Seite ist dann kuerzer als limit — kein Absturz", len(seite) == 2,
            f"{len(seite)} statt 3")

# ---------------------------------------------------------------------------
print("\nV9) Redis kaputt: Feed laeuft weiter")


class _KaputtesRedis:
    """Tut so, als waere Redis nicht erreichbar — beide Richtungen."""

    def get(self, *a, **k):
        raise redis.RedisError("Testausfall")

    def set(self, *a, **k):
        raise redis.RedisError("Testausfall")


echtes_redis = feed.redis_client
feed.redis_client = _KaputtesRedis()
try:
    print("  (die zwei Warnzeilen unten gehoeren dazu)")
    alt, _ = abruf(db, user, rerank=False, limit=5, skip=0)
    ohne, n = abruf(db, user, rerank=True, limit=5, skip=0)
    pruefen("Seite stimmt trotzdem", alt == ohne,
            f"{len(alt)} Posts" if alt == ohne else f"alt {ids(alt)} / neu {ids(ohne)}")
    pruefen("ohne Cache: 1 Anweisung (rechnet jedes Mal neu)", n == 1, f"{n} gezaehlt")
finally:
    feed.redis_client = echtes_redis

# ---------------------------------------------------------------------------
print("\nV10) Echtes Modell (Sim-Gewichte, nur in diesem Prozess)")
if not SIM_GEWICHTE.exists():
    print(f"  UEBERSPRUNGEN  {SIM_GEWICHTE} fehlt — erst python -m training.export")
else:
    gewichte_im_code = ranker.GEWICHTE
    ranker.GEWICHTE = SIM_GEWICHTE
    ranker._modell_laden.cache_clear()   # sonst bliebe das gemerkte None haengen
    try:
        pruefen("Modell geladen", ranker._modell_laden() is not None, str(SIM_GEWICHTE))

        # Alte Reihenfolge aus V8 (mit toter ID) darf hier nicht mitspielen.
        if redis_da:
            redis_client.delete(key)

        neu, n = abruf(db, user, rerank=True, limit=len(alle_alt), skip=0)
        pruefen("dieselben Posts wie der alte Weg", sorted(ids(neu)) == sorted(ids(alle_alt)),
                f"{len(neu)} Posts")
        pruefen("weiterhin 1 SQL-Anweisung", n == 1, f"{n} gezaehlt")

        # Gegenprobe: die Kandidaten selbst scoren und sortieren. Das now() weicht
        # um Millisekunden von dem im Abruf ab — fuer die Reihenfolge egal.
        zeilen = feed._kandidaten_holen(db, user, limit=FEED_CANDIDATE_LIMIT, offset=0,
                                        search="", local=False)
        scores = feed._lytir_scores(zeilen, user)
        erwartet = [z[0].id for s, z in sorted(zip(scores, zeilen),
                                               key=lambda p: p[0], reverse=True)]
        pruefen("Reihenfolge = absteigender Lytir-Score", ids(neu) == erwartet,
                "" if ids(neu) == erwartet else f"bekommen {ids(neu)} / erwartet {erwartet}")

        geaendert = ids(neu) != ids(alle_alt)
        print(f"  INFO    Reihenfolge gegenueber SQL {'geaendert' if geaendert else 'GLEICH'}")
        print(f"          SQL:   {ids(alle_alt)}")
        print(f"          Lytir: {ids(neu)}")

        # Zeit fuer 150 Kandidaten: die vorhandenen Zeilen so oft wiederholen,
        # bis 150 zusammen sind — gemessen wird build_features + Netz.
        if zeilen:
            voll = (zeilen * (FEED_CANDIDATE_LIMIT // len(zeilen) + 1))[:FEED_CANDIDATE_LIMIT]
            feed._lytir_scores(voll, user)   # aufwaermen
            laeufe = []
            for _ in range(20):
                t = time.perf_counter()
                feed._lytir_scores(voll, user)
                laeufe.append((time.perf_counter() - t) * 1000)
            laeufe.sort()
            print(f"  INFO    _lytir_scores fuer {len(voll)} Kandidaten: "
                  f"median {laeufe[10]:.1f} ms, max {laeufe[-1]:.1f} ms")
    finally:
        ranker.GEWICHTE = gewichte_im_code
        ranker._modell_laden.cache_clear()

# ---------------------------------------------------------------------------
if redis_da:
    for k in benutzte_keys:
        redis_client.delete(k)
db.close()

print("\n" + "=" * 75)
if len(alle_alt) < FEED_CANDIDATE_LIMIT:
    print(f"Hinweis: nur {len(alle_alt)} Kandidaten im Fenster — die Grenze bei "
          f"{FEED_CANDIDATE_LIMIT} ist mit diesen Daten NICHT geprueft.")
if SCHALTER_IM_CODE:
    print("ACHTUNG: LYTIR_RERANK_ENABLED steht im Code auf True — solange nur "
          "der Platzhalter rankt, zurueck auf False.")
if fehler:
    print(f"{len(fehler)} FEHLER: {fehler}")
    sys.exit(1)
print("Alles gruen: ohne Modell exakt der alte Feed, mit Modell nach Score sortiert.")
