"""Misst, was ein Feed-Abruf (GET /posts/) heute wirklich kostet.

Baseline VOR Lytir-6a. Alles hier ist nur lesend.

  M0  Aufwaermen (Verbindung, SQLAlchemy-Caches) — Zeiten ignorieren.
  M1  Feed wie heute:            limit=10,  skip=0
  M2  Seite 2:                   limit=10,  skip=10
  M3  Kandidatenmenge von 6a:    limit=150, skip=0

Gemessen wird nur die Endpoint-FUNKTION — ohne HTTP, ohne Auth, ohne die
Pydantic-Serialisierung nach PostOut. Interessant sind zwei Dinge: die Anzahl
der Anweisungen (taucht eine Form 150x auf, ist es N+1) und wie sich die Zeit
zwischen Warten-auf-Postgres und Python aufteilt.

Aufruf:  .\\venv\\Scripts\\python.exe scripts\\feed_query_test.py
"""
import sys
import time
from pathlib import Path

from collections import Counter
from contextlib import contextmanager


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import event, text  # noqa: E402

from app import models, schemas  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.routers.post import get_posts  # noqa: E402

# Jede ausgefuehrte SQL-Anweisung landet hier: (sql, dauer_in_sekunden).
protokoll: list[tuple[str, float]] = []

# Preis EINES Hin-und-Rueckwegs zur DB, unten per Leerlauf gemessen. Absolute
# Millisekunden schwanken je nach Netz um 25 % und mehr — Rundwege nicht.
# Deshalb ist das hier der Maßstab, in dem wir Laeufe vergleichen.
latenz_ms: float = 0.0


@event.listens_for(engine, "before_cursor_execute")
def _vorher(conn, cursor, statement, parameters, context, executemany):
    conn.info["start"] = time.perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _nachher(conn, cursor, statement, parameters, context, executemany):
    protokoll.append((statement, time.perf_counter() - conn.info["start"]))

@contextmanager
def messung(name: str):
    """Leert das Protokoll, stoppt mit, wertet danach aus."""
    protokoll.clear()
    t0 = time.perf_counter()
    try:
        yield
    finally:
        wanduhr = time.perf_counter() - t0
        sql_zeit = sum(dauer for _, dauer in protokoll)

        print(f"\n{name}")
        print(f"  Anweisungen : {len(protokoll)}")
        print(f"  SQL-Zeit    : {sql_zeit * 1000:7.1f} ms")
        print(f"  Gesamt      : {wanduhr * 1000:7.1f} ms"
              f"   (Python-Anteil {(wanduhr - sql_zeit) * 1000:.1f} ms)")
        if latenz_ms:
            print(f"  Rundwege    : {sql_zeit * 1000 / latenz_ms:7.1f}"
                  f"   (Leerlauf-Maßstab: {latenz_ms:.1f} ms pro Anweisung)")

        # Gleiche SQL-Form mehrfach = N+1. Genau das wollen wir sehen.
        formen = Counter(sql.split("\n")[0][:70] for sql, _ in protokoll)
        for form, anzahl in formen.most_common():
            print(f"  {anzahl:3}x  {form}")


# ---------------------------------------------------------------------------
# Messung. get_posts() wird direkt aufgerufen — die Depends() sind nur
# Default-Werte, solange FastAPI nicht dazwischensteht.
db = SessionLocal()

user = db.query(models.User).order_by(models.User.id).first()
if user is None:
    print("Keine User in der DB — nichts zu messen.")
    sys.exit(1)

print(f"Testuser: id={user.id}, location_id={user.location_id}")

with messung("M0) Aufwaermen — Zeiten ignorieren"):
    get_posts(db=db, current_user=user, limit=10, skip=0)

# Wie teuer ist eine Anweisung, die GAR NICHTS tut? Das ist der Preis pro
# Hin-und-Rueckweg zur DB. Alles darueber ist echte Arbeit von Postgres.
with messung("ML) Leerlauf: 3x SELECT 1 (reine Netzwerk-Latenz)"):
    for _ in range(3):
        db.execute(text("SELECT 1"))

# Maßstab fuer alle folgenden Messungen setzen. protokoll steht noch, es wird
# erst beim naechsten messung() geleert.
latenz_ms = sum(dauer for _, dauer in protokoll) * 1000 / len(protokoll)
print(f"  -> Maßstab  : {latenz_ms:.1f} ms pro Anweisung")

with messung("M1) Feed wie heute: limit=10, skip=0"):
    seite1 = get_posts(db=db, current_user=user, limit=10, skip=0)
print(f"  zurueck     : {len(seite1)} Posts")

# Die laengste Anweisung ist die Feed-Query. Am Ende ausgeben, um zu pruefen,
# ob die EXISTS-Subqueries korrelieren (posts.id von AUSSEN) oder ob posts
# nochmal im FROM der Subquery steht — letzteres waere ein stiller Bug.
feed_sql = max(protokoll, key=lambda eintrag: len(eintrag[0]))[0]

with messung("M2) Seite 2: limit=10, skip=10"):
    seite2 = get_posts(db=db, current_user=user, limit=10, skip=10)
print(f"  zurueck     : {len(seite2)} Posts")

with messung("M3) Kandidatenmenge von 6a: limit=150, skip=0"):
    kandidaten = get_posts(db=db, current_user=user, limit=150, skip=0)
print(f"  zurueck     : {len(kandidaten)} Posts")

# Wie stark maskiert die Identity Map das Lazy Loading? Gehoeren alle Posts
# demselben Autor, reicht EINE users-Query fuer alle. In Produktion wird
# daraus eine pro VERSCHIEDENEM Autor — die Messung untertreibt dann.
autoren = {p["post"].owner_id for p in kandidaten}
orte = {p["post"].location_id for p in kandidaten if p["post"].location_id}
print(f"  darin       : {len(autoren)} verschiedene Autoren, {len(orte)} verschiedene Orte")

# ---------------------------------------------------------------------------
# Gegenprobe. "Laeuft ohne Fehler" ist nicht "rechnet richtig": is_liked kommt
# jetzt als EXISTS-Spalte aus der DB statt aus einer Python-Menge. Also den
# alten Weg unabhaengig nachrechnen und vergleichen.
meine_votes = {
    row.post_id
    for row in db.query(models.Votes.post_id).filter(
        models.Votes.user_id == user.id
    ).all()
}
abweichungen = [
    p["post"].id
    for p in kandidaten
    if p["is_liked"] != (p["post"].id in meine_votes)
]

# Deckt der Test den Report-Filter ueberhaupt ab? Ohne eigene Reports sagt ein
# gruenes Ergebnis dort nichts aus — das muss man wissen, nicht hoffen.
meine_reports = db.query(models.Report).filter(
    models.Report.reporter_id == user.id,
    models.Report.post_id.isnot(None),
).count()

print("\nGegenprobe")
print(f"  is_liked    : {len(meine_votes)} Votes von User {user.id}, "
      f"{len(abweichungen)} Abweichungen {abweichungen}")
print(f"  Reports     : {meine_reports} gemeldete Posts von User {user.id}"
      + ("  <- Filter ist hier UNGETESTET" if meine_reports == 0 else ""))

db.close()

# ---------------------------------------------------------------------------
# M4 ist der ehrliche Abruf: FastAPI serialisiert nach dem return noch nach
# PostOut, und schemas.Post hat "owner: UserOut" und "location: LocationOut".
# Beides sind Lazy-Relationships -> der Zugriff loest Nachladen aus, und das
# passiert erst NACH get_posts(). M1 kann das per Bauart nicht sehen.
#
# Eigene Session, weil die alte die User-Objekte schon kennt (Identity Map)
# und das Nachladen dann ausbleiben wuerde.
db2 = SessionLocal()
with messung("M4) limit=10 MIT Serialisierung nach PostOut (wie echt)"):
    roh = get_posts(db=db2, current_user=user, limit=10, skip=0)
    fertig = [schemas.PostOut.model_validate(p) for p in roh]
print(f"  zurueck     : {len(fertig)} Posts")
db2.close()

print("\n" + "=" * 75)
print("Die Feed-Anweisung im Original (auf Korrelation pruefen):")
print("=" * 75)
print(feed_sql)

print("\nFazit-Hilfe:")
print("  - M1 vs M4: die Differenz ist das Lazy Loading der Serialisierung.")
print("  - Waechst die Zeit von M1 zu M3 kaum, ist die Kandidatenmenge billig.")
print("  - Nach 6a hier erneut messen und die Zahlen vergleichen.")

