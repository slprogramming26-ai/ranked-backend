"""Prueft den Wechsel auf Supavisors Transaktions-Modus (Phase 4, Schritt 4b).

Voraussetzung: DATABASE_PORT=6543 in der .env. Ports 8005 frei.
Alles hier ist NUR LESEND (SELECT pg_backend_pid(), GET /users/ fuer User 2).

  T1  Die .env zeigt wirklich auf 6543.
  T2  30 Clients halten GLEICHZEITIG eine offene Verbindung und fragen dann
      alle zugleich. Im Session-Modus wuerde ab dem 16. "max clients reached"
      kommen. Zaehlt ausserdem, wie viele echte Postgres-Prozesse (pg_backend_pid)
      die 30 sich geteilt haben -> deutlich weniger als 30 (gemessen: 16).
  T3  uvicorn mit --workers 4 wie spaeter in Produktion, 120 gleichzeitige
      GET /users/ -> alle 200, keine 500, kein Pooler-Fehler im Log.
"""
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SQLALCHEMY_DATABASE_URL  # noqa: E402
from app.oauth2 import create_access_token  # noqa: E402

PYTHON = str(ROOT / "venv" / "Scripts" / "python.exe")
PORT = 8005
CLIENTS = 30
REQUESTS = 120
WORKERS = 4

ergebnisse: list[bool] = []


def pruefe(name: str, ok: bool, info: str) -> None:
    print(f"  [{'OK' if ok else 'FEHLER'}] {name}: {info}")
    ergebnisse.append(ok)


# ---------------------------------------------------------------------------
print("T1) Konfiguration")
pruefe("T1 DATABASE_PORT ist 6543", str(settings.database_port) == "6543",
       f"steht auf {settings.database_port}")
if str(settings.database_port) != "6543":
    print("\nAbbruch: erst DATABASE_PORT=6543 in die .env, sonst testet das den Session-Modus.")
    sys.exit(1)

# ---------------------------------------------------------------------------
print(f"T2) {CLIENTS} Clients gleichzeitig offen, dann alle zugleich fragen")
# Eigene Engine OHNE Pool: jeder Thread bekommt garantiert eine eigene
# Client-Verbindung zu Supavisor (der App-Pool hat ja nur 15).
roh = create_engine(SQLALCHEMY_DATABASE_URL, poolclass=NullPool,
                    connect_args={"sslmode": "require"})
alle_offen = threading.Barrier(CLIENTS, timeout=60)
pids: list[int] = []
fehler: list[str] = []
sperre = threading.Lock()


def client() -> None:
    try:
        with roh.connect() as conn:
            alle_offen.wait()          # erst weiter, wenn ALLE 30 verbunden sind
            pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
            conn.commit()
            alle_offen.wait()          # und erst schliessen, wenn alle gefragt haben
            with sperre:
                pids.append(pid)
    except Exception as e:  # noqa: BLE001
        with sperre:
            fehler.append(f"{type(e).__name__}: {str(e).splitlines()[0][:120]}")
        try:
            alle_offen.abort()
        except Exception:  # noqa: BLE001
            pass


t0 = time.perf_counter()
threads = [threading.Thread(target=client) for _ in range(CLIENTS)]
for t in threads:
    t.start()
for t in threads:
    t.join()
roh.dispose()
dauer = time.perf_counter() - t0

pruefe(f"T2a alle {CLIENTS} gleichzeitig verbunden und beantwortet",
       len(pids) == CLIENTS and not fehler,
       f"{len(pids)} ok, {len(fehler)} Fehler, {dauer:.1f}s" + (f" | {fehler[0]}" if fehler else ""))
# Kriterium ist "weniger Prozesse als Clients" (= sie teilen sich Leitungen),
# nicht exakt 15: Supavisor faehrt im Transaktions-Modus empirisch 16 Prozesse
# bei Pool Size 15, stabil ueber mehrere Runden und langlebig (am 2026-09-19
# per pg_stat_activity geprueft) -> feste Obergrenze, kein Leck.
pruefe("T2b 30 Clients teilen sich wenige echte Postgres-Prozesse",
       0 < len(set(pids)) < CLIENTS, f"{len(set(pids))} verschiedene pg_backend_pid")

# ---------------------------------------------------------------------------
print(f"T3) uvicorn --workers {WORKERS}, {REQUESTS} gleichzeitige GET /users/")
logdatei = Path(tempfile.mkdtemp(prefix="pooler_test_")) / "uvicorn.log"
log = open(logdatei, "w")
proc = subprocess.Popen(
    [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
     "--port", str(PORT), "--workers", str(WORKERS)],
    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
)
try:
    frist = time.time() + 60
    while time.time() < frist:
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn ist beendet, siehe {logdatei}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    time.sleep(3)  # die uebrigen Worker nachkommen lassen

    token = create_access_token({"user_id": "2"})

    def anfrage(_: int) -> int:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/users/",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:  # noqa: BLE001
            return -1

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=REQUESTS) as ex:
        codes = list(ex.map(anfrage, range(REQUESTS)))
    dauer = time.perf_counter() - t0

    verteilung = {c: codes.count(c) for c in sorted(set(codes))}
    pruefe(f"T3a alle {REQUESTS} Requests -> 200", verteilung == {200: REQUESTS},
           f"{verteilung}, {dauer:.1f}s gesamt")
finally:
    proc.terminate()
    proc.wait(timeout=15)
    log.close()

logtext = logdatei.read_text(errors="replace")
treffer = [z for z in logtext.splitlines()
           if any(w in z for w in ("max clients", "OperationalError", "Traceback", "EMAXCONN"))]
pruefe("T3b kein Pooler-/DB-Fehler im uvicorn-Log", not treffer,
       "sauber" if not treffer else treffer[0][:160])

print(f"\n{sum(ergebnisse)}/{len(ergebnisse)} gruen.  uvicorn-Log: {logdatei}")
sys.exit(0 if all(ergebnisse) else 1)
