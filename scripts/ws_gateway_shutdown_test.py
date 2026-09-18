"""Testet das saubere Herunterfahren des Go-Gateways (Teil 3c der Strecke C).

Startet ws-gateway.exe SELBST, haengt drei Verbindungen dran (User 2 mit zwei
Geraeten, User 14 mit einem), schickt dem Prozess dann ein Beenden-Signal und
prueft, was dabei passiert:

  1. Vorbedingung: beide Presence-Eintraege stehen waehrend des Betriebs.
  2. Alle drei Sockets bekommen Close-Code 1001 mit "server shutting down"
     (StatusGoingAway -> der Client weiss, dass er sofort neu verbinden soll).
  3. ws:online:2 ist sofort weg, nicht erst nach 60 s TTL.
  4. ws:online:14 ist sofort weg.
  5. Der Prozess endet von allein, mit Code 0 und zuegig - nicht erst nach dem
     5-s-Timeout des Close-Handshakes.
  6. Das Log meldet die Sammelabmeldung.

Ohne 3c wuerde srv.Shutdown die WebSockets liegen lassen: die Clients haengen
bis in einen Timeout und alle Nutzer stehen bis zu 60 s faelschlich online.

Zum Signal: Windows kennt kein SIGTERM. Go bildet die Konsolen-Ereignisse
CTRL_C und CTRL_BREAK beide auf os.Interrupt ab, aber CREATE_NEW_PROCESS_GROUP
schaltet CTRL_C fuer das Kind ab - deshalb CTRL_BREAK. Auf Railway ist es
spaeter ein echtes SIGTERM, das durch denselben Kanal in main.go laeuft.

Start:  venv\\Scripts\\python.exe scripts\\ws_gateway_shutdown_test.py

Voraussetzung: Port 8080 ist FREI (das Skript startet den Dienst selbst) und
ws-gateway.exe ist frisch gebaut (go build -o ws-gateway.exe .).
"""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.oauth2 import create_access_token
from app.redis_client import redis_client

GATEWAY_DIR = Path(r"C:\Users\Sebastian\Documents\Programmieren\Projekte\ranked\ws-gateway")
EXE = GATEWAY_DIR / "ws-gateway.exe"

BASE = "ws://127.0.0.1:8080/ws/chat"
PORT = 8080

# User 2 haengt mit zwei Geraeten dran - so beweist Test 6 nebenbei, dass
# closeAll je Nutzer nur EINE Abmeldung erzeugt, egal wie viele Sockets.
USER_A = 2
USER_B = 14
KEY_A = f"ws:online:{USER_A}"
KEY_B = f"ws:online:{USER_B}"

# Ab hier gilt das Herunterfahren als haengengeblieben.
FRIST = 15
# Laenger als das hiesse: Go hat auf den Close-Handshake ins Leere gewartet.
ZUEGIG = 5.0

ergebnisse: list[bool] = []


def pruefe(label: str, ok: bool, detail: str) -> None:
    ergebnisse.append(ok)
    print(f"[{'OK    ' if ok else 'FEHLER'}] {label}: {detail}")


def port_frei() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) != 0


def warte_auf_start(p: subprocess.Popen, sekunden: float = 10.0) -> bool:
    ende = time.monotonic() + sekunden
    while time.monotonic() < ende:
        if p.poll() is not None:
            return False
        if not port_frei():
            return True
        time.sleep(0.1)
    return False


async def erwarte_close(ws) -> tuple[int | None, str]:
    """Wartet auf das Ende der Verbindung und gibt Code und Grund zurueck."""
    try:
        nachricht = await asyncio.wait_for(ws.recv(), timeout=FRIST)
        return None, f"unerwartete Nachricht statt Close: {nachricht!r}"
    except ConnectionClosed as e:
        rcvd = getattr(e, "rcvd", None)
        if rcvd is None:
            return None, "geschlossen ohne Close-Frame"
        return rcvd.code, rcvd.reason
    except asyncio.TimeoutError:
        return None, f"Socket blieb {FRIST}s offen"


async def main() -> None:
    print(f"Ziel: {BASE}  (User {USER_A} mit 2 Geraeten, User {USER_B} mit 1)\n")

    if not EXE.exists():
        print(f"ABBRUCH: {EXE} fehlt. Erst bauen: go build -o ws-gateway.exe .")
        sys.exit(2)

    if not port_frei():
        print(f"ABBRUCH: Port {PORT} ist belegt. Dieses Skript startet den")
        print("         Dienst selbst - das laufende Gateway bitte beenden.")
        sys.exit(2)

    # Reste aus frueheren Laeufen wuerden Test 1 verfaelschen.
    redis_client.delete(KEY_A, KEY_B)

    p = subprocess.Popen(
        [str(EXE)],
        cwd=str(GATEWAY_DIR),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if not warte_auf_start(p):
        print("ABBRUCH: Gateway ist nicht hochgekommen.")
        p.kill()
        print(p.communicate()[0])
        sys.exit(2)

    token_a = create_access_token({"user_id": str(USER_A)})
    token_b = create_access_token({"user_id": str(USER_B)})

    handy = await connect(f"{BASE}?token={token_a}")
    tablet = await connect(f"{BASE}?token={token_a}")
    anderer = await connect(f"{BASE}?token={token_b}")
    sockets = [("handy", handy), ("tablet", tablet), (f"user{USER_B}", anderer)]

    await asyncio.sleep(0.5)  # Go braucht einen Moment fuer das SETEX

    pruefe(
        "01 Vorbedingung: beide Nutzer sind online",
        redis_client.get(KEY_A) is not None and redis_client.get(KEY_B) is not None,
        f"{KEY_A}={redis_client.get(KEY_A)!r}, {KEY_B}={redis_client.get(KEY_B)!r}",
    )

    # Erst die Warte-Tasks starten, DANN das Signal: die Event-Loop muss
    # laufen, sonst antworten die Clients nicht auf den Close-Frame und Go
    # liefe in seinen 5-s-Timeout - der Test wuerde dann Geduld messen
    # statt Verhalten.
    warten = [asyncio.create_task(erwarte_close(ws)) for _, ws in sockets]
    await asyncio.sleep(0.1)

    start = time.monotonic()
    os.kill(p.pid, signal.CTRL_BREAK_EVENT)

    abschluesse = await asyncio.gather(*warten)

    for (name, _), (code, grund) in zip(sockets, abschluesse):
        pruefe(
            f"02 {name}: Close 1001 'server shutting down'",
            code == 1001 and grund == "server shutting down",
            f"Code {code}, Grund {grund!r}",
        )

    out, _ = await asyncio.to_thread(p.communicate, None, FRIST)
    dauer = time.monotonic() - start

    # Erst jetzt nach Redis schauen: der Prozess ist tot, also kann kein
    # spaeter keepPresence-Tick den Eintrag noch einmal setzen.
    wert_a = redis_client.get(KEY_A)
    wert_b = redis_client.get(KEY_B)

    pruefe(
        f"03 {KEY_A} sofort geloescht",
        wert_a is None,
        f"wert={wert_a!r}  (nicht None hiesse: bis zu 60 s falsch online)",
    )
    pruefe(
        f"04 {KEY_B} sofort geloescht",
        wert_b is None,
        f"wert={wert_b!r}",
    )
    pruefe(
        "05 Prozess endet von allein, Code 0, zuegig",
        p.returncode == 0 and dauer < ZUEGIG,
        f"exit={p.returncode} nach {dauer:.2f}s  (>= {ZUEGIG}s hiesse: in den Handshake-Timeout gelaufen)",
    )

    # Drei Sockets, aber nur zwei Nutzer: die beiden Zahlen muessen sich
    # unterscheiden, sonst zaehlt closeAll wieder Nutzer statt Sockets.
    zeile = next((z for z in out.splitlines() if "abgemeldet" in z), "")
    pruefe(
        "06 Log meldet 3 Sockets und 2 Nutzer",
        "3 verbindungen geschlossen" in zeile and "2 nutzer abgemeldet" in zeile,
        zeile.strip() or "keine Abmeldezeile im Log",
    )

    print("\n--- Go-Log ---")
    for z in out.splitlines():
        print("  |", z)

    bestanden = sum(ergebnisse)
    print(f"\n{bestanden}/{len(ergebnisse)} gruen")


asyncio.run(main())
sys.exit(0 if all(ergebnisse) else 1)
