"""Misst, was der Umweg ueber den Go-Gateway kostet -- Weg A gegen Weg B.

    Weg A:  Client --WS--> Go-Gateway --HTTPS--> FastAPI --> Supabase --> ack
    Weg B:  Client --WS-----------------------> FastAPI --> Supabase --> ack

Beide Wege laufen von DIESEM Rechner gegen Railway. Alles, was sie gemeinsam
haben (deine Leitung, der Weg nach Amsterdam, der Supabase-Insert), steckt in
beiden Zahlen und faellt beim Abziehen heraus. Was uebrig bleibt, ist der
zusaetzliche Sprung Go -> FastAPI ueber die oeffentliche Kante plus Gos eigene
Verarbeitung.

Gemessen wird pro Nachricht die Zeit von 'abgeschickt' bis 'ack ist da'.

Dazu eine Grundlinie: ein GET auf / des Backends (keine Datenbank, kein Go).
Sie zeigt, wie viel der Gesamtzeit blosse Netzlaufzeit ist.

WICHTIG -- die erste Nachricht je Weg wird GETRENNT ausgewiesen: darin steckt
einmalig der TCP- und TLS-Handschlag Go->FastAPI. Danach haelt Go die Verbindung
offen (MaxIdleConnsPerHost = 100 in backend.go), deshalb ist der Dauerbetrieb die
ehrlichere Zahl -- und der Unterschied zwischen beiden ist genau das Argument
dafuer, warum 'oeffentlich' hier wenig kostet.

Taktung: 0,3 s Pause zwischen den Nachrichten, sonst greift ratelimit.go
(5/s) auf Weg A und wir wuerden die Bremse messen statt die Leitung. Weg B
bekommt dieselbe Pause, damit der Vergleich fair bleibt.

Start:
    venv\\Scripts\\python.exe scripts\\ws_gateway_speed_test.py <gateway> <backend>

    <gateway>  z.B. ranked-ws-gateway-production.up.railway.app
    <backend>  z.B. ranked-production.up.railway.app

Schreibt echte DB-Zeilen: (AUFWAERM + MESSUNGEN) * 2, standardmaessig 36.
"""

import asyncio
import json
import statistics
import sys
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect

from app.oauth2 import create_access_token

SENDER = 2
RECIPIENT = 14

AUFWAERM = 3     # werden verworfen (bzw. die erste separat ausgewiesen)
MESSUNGEN = 15
PAUSE = 0.3      # ueber 1/5 s, damit ratelimit.go nicht mitmisst


def ws_url(roh: str) -> str:
    roh = roh.strip().rstrip("/")
    if roh.startswith("http://"):
        roh = "ws://" + roh[len("http://"):]
    elif roh.startswith("https://"):
        roh = "wss://" + roh[len("https://"):]
    elif not roh.startswith(("ws://", "wss://")):
        roh = "wss://" + roh
    return roh if roh.endswith("/ws/chat") else roh + "/ws/chat"


def http_url(roh: str) -> str:
    roh = roh.strip().rstrip("/")
    if not roh.startswith(("http://", "https://")):
        roh = "https://" + roh
    return roh


async def miss_weg(name: str, url: str, token: str) -> tuple[float, list[float]]:
    """Gibt (erste Nachricht in ms, Liste der Dauerbetrieb-Messungen in ms) zurueck."""
    dauern: list[float] = []
    erste = 0.0

    async with connect(f"{url}?token={token}") as ws:
        for i in range(AUFWAERM + MESSUNGEN):
            payload = {
                "kind": "dm",
                "to": RECIPIENT,
                "message": f"speedtest {name} {i}",
                "client_msg_id": str(uuid.uuid4()),
            }
            start = time.perf_counter()
            await ws.send(json.dumps(payload))
            roh = await asyncio.wait_for(ws.recv(), timeout=30.0)
            dauer = (time.perf_counter() - start) * 1000

            antwort = json.loads(roh)
            if antwort.get("kind") != "ack":
                print(f"  ABBRUCH auf {name}: {antwort}")
                return erste, dauern

            if i == 0:
                erste = dauer          # kalt: enthaelt den TLS-Handschlag
            elif i >= AUFWAERM:
                dauern.append(dauer)

            await asyncio.sleep(PAUSE)

    return erste, dauern


def grundlinie(url: str, n: int = 10) -> list[float]:
    """GET / gegen das Backend -- reine Netzlaufzeit, keine DB, kein Go."""
    werte = []
    for i in range(n):
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(url + "/", timeout=15) as r:
                r.read()
        except Exception as e:
            print(f"  Grundlinie fehlgeschlagen: {type(e).__name__}: {e}")
            return werte
        if i > 0:                      # erste Anfrage traegt den TLS-Handschlag
            werte.append((time.perf_counter() - start) * 1000)
    return werte


def zeile(label: str, werte: list[float]) -> str:
    if not werte:
        return f"{label:<28} (keine Messwerte)"
    return (f"{label:<28} Median {statistics.median(werte):6.1f} ms   "
            f"Mittel {statistics.mean(werte):6.1f}   "
            f"min {min(werte):6.1f}   max {max(werte):6.1f}")


async def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)

    gateway = ws_url(sys.argv[1])
    backend_ws = ws_url(sys.argv[2])
    backend_http = http_url(sys.argv[2])
    token = create_access_token({"user_id": str(SENDER)})

    print(f"Weg A (ueber Go):   {gateway}")
    print(f"Weg B (direkt):     {backend_ws}")
    print(f"Grundlinie:         {backend_http}/")
    print(f"\n{AUFWAERM} Aufwaermnachrichten + {MESSUNGEN} Messungen je Weg, "
          f"{PAUSE}s Pause.\n")

    print("Grundlinie laeuft ...")
    basis = grundlinie(backend_http)

    print("Weg A laeuft ...")
    a_erste, a = await miss_weg("A", gateway, token)

    print("Weg B laeuft ...")
    b_erste, b = await miss_weg("B", backend_ws, token)

    print("\n" + "=" * 78)
    print(zeile("Grundlinie (GET /)", basis))
    print(zeile("Weg B  direkt zu FastAPI", b))
    print(zeile("Weg A  ueber Go-Gateway", a))
    print("=" * 78)

    if a and b:
        d_median = statistics.median(a) - statistics.median(b)
        d_mittel = statistics.mean(a) - statistics.mean(b)
        anteil = d_median / statistics.median(b) * 100
        print(f"\nAufpreis fuer den Umweg ueber Go:")
        print(f"   Median {d_median:+.1f} ms   Mittel {d_mittel:+.1f} ms   "
              f"= {anteil:+.1f} % auf Weg B")

    print(f"\nErste Nachricht (kalt, mit TLS-Handschlag):")
    print(f"   Weg A {a_erste:.1f} ms   Weg B {b_erste:.1f} ms")
    if a:
        print(f"   Weg A danach im Dauerbetrieb: {statistics.median(a):.1f} ms "
              f"-> der Handschlag faellt nur EINMAL an, nicht pro Nachricht.")


asyncio.run(main())
