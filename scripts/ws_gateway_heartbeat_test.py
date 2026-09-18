"""Testet das Ping/Pong-Zeitlimit des Go-Gateways.

Ein Handy im Funkloch oder eine weggewischte App schickt kein Close-Frame -
fuer den Server sieht die Verbindung noch stundenlang gesund aus. Der
heartbeat in presence.go pingt deshalb alle 25 s und trennt, wenn binnen
10 s kein Pong kommt.

Das Skript prueft beide Seiten davon:

  1. Vorbedingung: der stumme Client ist verbunden und steht online.
  2. Go schickt einen Ping (Opcode 0x9) - und zwar nach etwa 25 s.
  3. Ohne Pong wird die Verbindung getrennt, spaetestens nach 25+10 s.
  4. ws:online:{stumm} ist danach weg - das Aufraeumen laeuft wie bei
     einem normalen Abgang.
  5. Gegenprobe: ein hoeflicher Client, der die ganze Zeit antwortet, ist
     unbeschadet noch verbunden und online. Ohne diesen Fall koennte der
     heartbeat schlicht jede Verbindung kappen und die Tests 2-4 waeren
     trotzdem gruen.

Der stumme Client ist ein roher TCP-Socket mit von Hand gesprochenem
WebSocket-Handshake: die websockets-Bibliothek ist zu hoeflich, sie
beantwortet Pings automatisch und laesst sich nicht davon abbringen.

Start:  venv\\Scripts\\python.exe scripts\\ws_gateway_heartbeat_test.py

Dauert gut 40 Sekunden - der Takt des Gateways gibt das vor.

Voraussetzung: ws-gateway laeuft auf 127.0.0.1:8080 gegen dasselbe Redis
wie dieses Backend.
"""

import asyncio
import base64
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect

from app.oauth2 import create_access_token
from app.redis_client import redis_client

HOST = "127.0.0.1"
PORT = 8080
BASE = f"ws://{HOST}:{PORT}/ws/chat"

USER_STUMM = 14
USER_HOEFLICH = 2
KEY_STUMM = f"ws:online:{USER_STUMM}"
KEY_HOEFLICH = f"ws:online:{USER_HOEFLICH}"

# Aus presence.go: presenceRefresh = 25s, pongFrist = 10s.
TAKT = 25
FRIST = 10
# Grosszuegig, damit ein langsamer Rechner den Test nicht rot macht.
MAX_WARTEN = TAKT + FRIST + 20

OPCODE_PING = 0x9

ergebnisse: list[bool] = []


def pruefe(label: str, ok: bool, detail: str) -> None:
    ergebnisse.append(ok)
    print(f"[{'OK    ' if ok else 'FEHLER'}] {label}: {detail}")


def stummer_client(token: str) -> dict:
    """Verbindet per rohem Socket und antwortet dann auf gar nichts.

    Gibt zurueck, wann der erste Ping kam und wann Go die Verbindung
    geschlossen hat - beides in Sekunden seit dem Handshake.
    """
    s = socket.create_connection((HOST, PORT), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    anfrage = (
        f"GET /ws/chat?token={token} HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    s.sendall(anfrage.encode("ascii"))

    kopf = b""
    while b"\r\n\r\n" not in kopf:
        stueck = s.recv(4096)
        if not stueck:
            break
        kopf += stueck

    ergebnis: dict = {
        "handshake": kopf.split(b"\r\n", 1)[0].decode("ascii", "replace"),
        "ping_nach": None,
        "getrennt_nach": None,
    }
    if "101" not in ergebnis["handshake"]:
        s.close()
        return ergebnis

    start = time.monotonic()
    s.settimeout(MAX_WARTEN)

    # Ab hier wird nur noch gelesen. Kein Pong, kein Close, gar nichts -
    # genau das, was ein Handy im Funkloch auch tut.
    rest = kopf.split(b"\r\n\r\n", 1)[1]
    try:
        while True:
            if rest:
                daten, rest = rest, b""
            else:
                daten = s.recv(4096)
            if not daten:
                ergebnis["getrennt_nach"] = time.monotonic() - start
                break
            if ergebnis["ping_nach"] is None and (daten[0] & 0x0F) == OPCODE_PING:
                ergebnis["ping_nach"] = time.monotonic() - start
    except socket.timeout:
        pass
    except OSError:
        # Go schliesst mit CloseNow, das kann als harter Abbruch ankommen.
        ergebnis["getrennt_nach"] = time.monotonic() - start
    finally:
        s.close()

    return ergebnis


async def main() -> None:
    print(f"Ziel: {BASE}")
    print(f"stumm: user {USER_STUMM}   hoeflich: user {USER_HOEFLICH}")
    print(f"erwartet: Ping nach ~{TAKT}s, Trennen nach ~{TAKT + FRIST}s\n")

    redis_client.delete(KEY_STUMM, KEY_HOEFLICH)

    token_stumm = create_access_token({"user_id": str(USER_STUMM)})
    token_hoeflich = create_access_token({"user_id": str(USER_HOEFLICH)})

    # Der hoefliche Client laeuft im asyncio-Teil weiter, waehrend der
    # stumme in einem Thread haengt - nur so antwortet er auch wirklich.
    hoeflich = await connect(f"{BASE}?token={token_hoeflich}")
    await asyncio.sleep(0.5)

    aufgabe = asyncio.create_task(asyncio.to_thread(stummer_client, token_stumm))
    await asyncio.sleep(1.0)

    pruefe(
        "01 Vorbedingung: stummer Client ist verbunden und online",
        redis_client.get(KEY_STUMM) is not None,
        f"{KEY_STUMM}={redis_client.get(KEY_STUMM)!r}",
    )

    print(f"\n  ... warte auf den Heartbeat (bis zu {MAX_WARTEN}s)\n")
    r = await aufgabe

    ping_nach = r["ping_nach"]
    getrennt_nach = r["getrennt_nach"]

    pruefe(
        "02 Go schickt einen Ping",
        ping_nach is not None and TAKT - 5 <= ping_nach <= TAKT + 5,
        f"Ping nach {ping_nach if ping_nach is None else round(ping_nach, 1)}s"
        f"  (erwartet ~{TAKT}s; None hiesse: es wird gar nicht gepingt)",
    )
    pruefe(
        "03 ohne Pong wird getrennt",
        getrennt_nach is not None and getrennt_nach <= TAKT + FRIST + 5,
        f"getrennt nach {getrennt_nach if getrennt_nach is None else round(getrennt_nach, 1)}s"
        f"  (erwartet ~{TAKT + FRIST}s; None hiesse: der Socket lebt weiter)",
    )

    await asyncio.sleep(0.5)  # Go braucht einen Moment fuers DEL
    wert_stumm = redis_client.get(KEY_STUMM)
    pruefe(
        "04 Presence des stummen Clients ist aufgeraeumt",
        wert_stumm is None,
        f"{KEY_STUMM}={wert_stumm!r}  (nicht None hiesse: Socket weg, Eintrag bleibt)",
    )

    # Lebt der hoefliche Client noch? Ein echter Austausch beweist es
    # besser als das blosse Fehlen einer Ausnahme.
    try:
        await hoeflich.send('{"kind":"kaputt"}')
        antwort = await asyncio.wait_for(hoeflich.recv(), timeout=5)
        lebt = "error" in antwort
        detail = f"antwortet noch: {antwort[:60]}"
    except Exception as e:
        lebt = False
        detail = f"Verbindung ist tot: {e!r}"

    pruefe("05 hoeflicher Client lebt unbeschadet weiter", lebt, detail)
    pruefe(
        "06 ... und steht weiterhin online",
        redis_client.get(KEY_HOEFLICH) is not None,
        f"{KEY_HOEFLICH}={redis_client.get(KEY_HOEFLICH)!r}",
    )

    await hoeflich.close()

    bestanden = sum(ergebnisse)
    print(f"\n{bestanden}/{len(ergebnisse)} gruen")


asyncio.run(main())
sys.exit(0 if all(ergebnisse) else 1)
