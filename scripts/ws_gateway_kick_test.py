"""Testet den WS-Rauswurf gesperrter User (Rollen & Moderation, Phase 7).

Ruft dieselben Funktionen auf wie ban_user/unban_user (publisher.kick_banned
und lift_ban) und schaut, was das Go-Gateway daraus macht:

  1. Ohne Sperre: Verbindung kommt zustande und bleibt offen.
  2. kick_banned: der offene Socket wird mit 4003 "account suspended" geschlossen.
  3. ws:banned:{id} steht mit Verfallszeit (SETEX, nicht SET).
  4. Neuverbinden mit demselben, noch gueltigen Token: sofort wieder 4003.
  5. lift_ban: Riegel ist weg, Neuverbinden klappt wieder.

Start:  venv\\Scripts\\python.exe scripts\\ws_gateway_kick_test.py

Voraussetzung: ws-gateway laeuft auf 127.0.0.1:8080 gegen dasselbe Redis
wie dieses Backend (.env in beiden Repos).

Bewusst eine Fantasie-User-ID: Go prueft Tokens ohne DB, und ein Kick auf
einen echten User wuerde ihn auch aus jedem anderen Gateway am selben Redis
werfen.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.oauth2 import create_access_token
from app.redis_client import redis_client
from app.ws.publisher import BANNED_TTL_SECONDS, kick_banned, lift_ban, _banned_key

BASE = "ws://127.0.0.1:8080/ws/chat"
USER_ID = 999999
KEY = _banned_key(USER_ID)

CODE_GESPERRT = 4003
GRUND_GESPERRT = "account suspended"

# So lange gilt eine Verbindung als "bleibt offen", wenn nichts kommt.
OFFEN_FRIST = 1.5

ergebnisse: list[bool] = []


def pruefe(label: str, ok: bool, detail: str) -> None:
    ergebnisse.append(ok)
    print(f"  [{'ok' if ok else 'FEHLER'}] {label}")
    print(f"         {detail}")


async def warte_auf_ende(ws, frist: float):
    """None, wenn der Socket innerhalb der Frist offen bleibt,
    sonst (code, reason) des Close-Frames vom Server."""
    try:
        nachricht = await asyncio.wait_for(ws.recv(), frist)
    except asyncio.TimeoutError:
        return None
    except ConnectionClosed as e:
        if e.rcvd is None:
            return ("kein close-frame", None)
        return (e.rcvd.code, e.rcvd.reason)
    return ("unerwartete nachricht", nachricht)


async def main() -> None:
    print(f"Ziel: {BASE}")
    print(f"Redis-Riegel: {KEY}\n")

    # Vorbedingung: ein Rest aus einem abgebrochenen Lauf wuerde Test 1 verfaelschen.
    if redis_client.delete(KEY):
        print("Hinweis: alter Riegel lag noch da und wurde geloescht.\n")

    token = create_access_token({"user_id": str(USER_ID)})

    try:
        async with connect(f"{BASE}?token={token}") as ws:
            ende = await warte_auf_ende(ws, OFFEN_FRIST)
            pruefe(
                "1 Ohne Sperre bleibt die Verbindung offen",
                ende is None,
                f"ende={ende!r}  (None = offen)",
            )

            kick_banned(USER_ID)
            ende = await warte_auf_ende(ws, 5)
            pruefe(
                "2 Offener Socket wird mit 4003 geschlossen",
                ende == (CODE_GESPERRT, GRUND_GESPERRT),
                f"ende={ende!r}  (None hiesse: Go hat ws:kick nicht verarbeitet)",
            )

        ttl = redis_client.ttl(KEY)
        pruefe(
            "3 Riegel steht mit Verfallszeit",
            BANNED_TTL_SECONDS - 10 <= ttl <= BANNED_TTL_SECONDS,
            f"ttl={ttl}s, erwartet ~{BANNED_TTL_SECONDS}s  (-1 hiesse: SET ohne Ablauf)",
        )

        async with connect(f"{BASE}?token={token}") as ws:
            ende = await warte_auf_ende(ws, 5)
            pruefe(
                "4 Neuverbinden mit gueltigem Token wird mit 4003 abgelehnt",
                ende == (CODE_GESPERRT, GRUND_GESPERRT),
                f"ende={ende!r}  (None hiesse: der Riegel in handleWS greift nicht)",
            )

        lift_ban(USER_ID)
        pruefe(
            "5a Riegel ist nach lift_ban weg",
            not redis_client.exists(KEY),
            f"exists={redis_client.exists(KEY)}",
        )

        async with connect(f"{BASE}?token={token}") as ws:
            ende = await warte_auf_ende(ws, OFFEN_FRIST)
            pruefe(
                "5b Nach dem Entsperren bleibt die Verbindung wieder offen",
                ende is None,
                f"ende={ende!r}  (None = offen)",
            )
    finally:
        # Egal wo es knallt: keinen Riegel in Redis liegen lassen.
        redis_client.delete(KEY)

    bestanden = sum(ergebnisse)
    print(f"\n{bestanden}/{len(ergebnisse)} bestanden")


asyncio.run(main())
sys.exit(0 if ergebnisse and all(ergebnisse) else 1)
