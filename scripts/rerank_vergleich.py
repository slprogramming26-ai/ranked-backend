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

Schreibt nur die eigenen Cache-Keys in Redis und loescht sie am Ende wieder.
Die DB wird nur gelesen.

Aufruf:  .\\venv\\Scripts\\python.exe scripts\\rerank_vergleich.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import redis  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app import feed, models  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.ranking_config import FEED_CANDIDATE_LIMIT, FEED_ORDER_CACHE_TTL  # noqa: E402
from app.redis_client import redis_client  # noqa: E402
from app.routers.post import get_posts  # noqa: E402

if not hasattr(feed, "LYTIR_RERANK_ENABLED"):
    print("feed.py kennt LYTIR_RERANK_ENABLED noch nicht — erst 5c eintippen.")
    sys.exit(1)

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
print("Alles gruen: der Rerank-Weg liefert mit Platzhalter exakt den alten Feed.")
