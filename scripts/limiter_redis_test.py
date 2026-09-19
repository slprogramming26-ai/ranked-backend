"""Prueft den slowapi-Limiter auf Redis (Phase 4, Schritt 3).

Drei Faelle, jeder mit frisch gestarteten uvicorn-Prozessen:
  A) Zwei Prozesse, EIN Redis: Logins abwechselnd an Prozess 1 und 2 -> der 6.
     muss 429 sein, obwohl jeder Prozess selbst erst 2-3 Logins gesehen hat.
     Beweist: der Zaehler liegt in Redis, nicht im Prozess.
  B) Redis tot (Port ohne Dienst): kein 500, sondern 401 und ab dem 6. 429
     (in_memory_fallback zaehlt im RAM weiter).
  C) Redis haengt (nimmt die Verbindung an, antwortet nie): wie B, aber der
     Timeout muss greifen, statt dass der Login endlos haengt.

Login mit einer E-Mail, die es nicht gibt -> nur ein SELECT gegen die DB,
nichts wird geschrieben. Voraussetzungen: lokales Redis laeuft (.env),
Ports 8001-8004 und 6398/6399 frei. Loescht vor und nach Fall A die
LIMITS*-Eintraege im LOKALEN Redis (nur Limiter-Zaehler, sonst nichts).
"""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.redis_client import redis_client  # noqa: E402

PYTHON = str(ROOT / "venv" / "Scripts" / "python.exe")
LOGDIR = Path(tempfile.mkdtemp(prefix="limiter_test_"))
FORM = urllib.parse.urlencode(
    {"username": "limiter-test@example.invalid", "password": "falsch"}
).encode()

ergebnisse: list[bool] = []


def pruefe(name: str, ok: bool, info: str) -> None:
    print(f"  [{'OK' if ok else 'FEHLER'}] {name}: {info}")
    ergebnisse.append(ok)


def limits_leeren() -> None:
    keys = redis_client.keys("LIMITS*")
    if keys:
        redis_client.delete(*keys)


def starte(port: int, redis_url: str | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    if redis_url:
        env["REDIS_URL"] = redis_url  # Env-Variable sticht .env
    log = open(LOGDIR / f"uvicorn_{port}.log", "w")
    p = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    frist = time.time() + 30
    while time.time() < frist:
        if p.poll() is not None:
            raise RuntimeError(f"uvicorn {port} ist beendet, siehe {log.name}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            return p
        except Exception:
            time.sleep(0.3)
    p.terminate()
    raise RuntimeError(f"uvicorn {port} kam nicht hoch, siehe {log.name}")


def stoppe(*prozesse: subprocess.Popen) -> None:
    for p in prozesse:
        p.terminate()
    for p in prozesse:
        p.wait(timeout=10)


def login(port: int) -> tuple[int, float]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/login", data=FORM, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    return code, time.perf_counter() - t0


def haengendes_redis(port: int, stop: threading.Event) -> None:
    """Nimmt TCP-Verbindungen an und sagt nie ein Wort."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen()
    srv.settimeout(0.2)
    offen = []
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
            offen.append(conn)
        except socket.timeout:
            pass
    for c in offen:
        c.close()
    srv.close()


def zeile(reihe: list[tuple[int, int, float]]) -> str:
    return "  ".join(f"{port}->{code} ({dauer:.2f}s)" for port, code, dauer in reihe)


# ---------------------------------------------------------------------------
print("A) Zwei Prozesse teilen sich EINEN Zaehler in Redis")
limits_leeren()
p1, p2 = starte(8001), starte(8002)
try:
    reihe = []
    for i in range(6):
        port = 8001 if i % 2 == 0 else 8002
        reihe.append((port, *login(port)))
    codes = [c for _, c, _ in reihe]
    print(f"     {zeile(reihe)}")
    pruefe("A1 Logins 1-5 durchgelassen", codes[:5] == [401] * 5, str(codes[:5]))
    pruefe("A2 Login 6 an Prozess 8002 -> 429", codes[5] == 429,
           f"{codes[5]} (8002 selbst hatte erst 2 gesehen)")
    keys = redis_client.keys("LIMITS*")
    wert = redis_client.get(keys[0]) if keys else None
    ttl = redis_client.ttl(keys[0]) if keys else None
    pruefe("A3 Zaehler liegt in Redis", len(keys) == 1 and wert is not None and int(wert) >= 5,
           f"{keys} = {wert}, TTL {ttl}s")
finally:
    stoppe(p1, p2)
    limits_leeren()

# ---------------------------------------------------------------------------
print("B) Redis tot -> Ersatz im RAM statt 500")
p = starte(8003, "redis://127.0.0.1:6399")
try:
    reihe = [(8003, *login(8003)) for _ in range(6)]
    codes = [c for _, c, _ in reihe]
    print(f"     {zeile(reihe)}")
    pruefe("B1 kein 500, Logins 1-5 -> 401", codes[:5] == [401] * 5, str(codes[:5]))
    pruefe("B2 Login 6 -> 429 (RAM zaehlt weiter)", codes[5] == 429, str(codes[5]))
    pruefe("B3 lokales Redis unberuehrt", redis_client.keys("LIMITS*") == [],
           "Env-Variable hat .env wirklich uebersteuert")
finally:
    stoppe(p)

# ---------------------------------------------------------------------------
print("C) Redis haengt -> Timeout greift, Login haengt nicht endlos")
stop = threading.Event()
t = threading.Thread(target=haengendes_redis, args=(6398, stop), daemon=True)
t.start()
p = starte(8004, "redis://127.0.0.1:6398")
try:
    reihe = [(8004, *login(8004)) for _ in range(6)]
    codes = [c for _, c, _ in reihe]
    dauern = [d for _, _, d in reihe]
    print(f"     {zeile(reihe)}")
    pruefe("C1 kein 500, Logins 1-5 -> 401", codes[:5] == [401] * 5, str(codes[:5]))
    pruefe("C2 Login 6 -> 429", codes[5] == 429, str(codes[5]))
    pruefe("C3 erster Login wartet begrenzt", dauern[0] < 15, f"{dauern[0]:.2f}s")
    pruefe("C4 danach wieder schnell (RAM-Modus)", max(dauern[1:]) < 1.5,
           f"max {max(dauern[1:]):.2f}s")
finally:
    stoppe(p)
    stop.set()
    t.join(timeout=5)

print(f"\n{sum(ergebnisse)}/{len(ergebnisse)} gruen.  uvicorn-Logs: {LOGDIR}")
sys.exit(0 if all(ergebnisse) else 1)
