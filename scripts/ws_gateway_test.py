"""Testet den Verbindungsaufbau des Go-Gateways (Strecke A, PROTOCOL.md 4.1).

Prueft nur den Handshake: gueltiges Token -> Verbindung steht,
ungueltiges/abgelaufenes Token -> Close 1008.

Start:  venv\\Scripts\\python.exe scripts\\ws_gateway_test.py
Voraussetzung: ws-gateway laeuft auf 127.0.0.1:8080.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.oauth2 import create_access_token

BASE = "ws://127.0.0.1:8080/ws/chat"
USER_ID = 2

# Aus scripts/ws_load.py, exp lag am 2026-08-29 -> laengst abgelaufen.
ABGELAUFEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJ1c2VyX2lkIjoiMSIsImV4cCI6MTc4ODAyNjg2N30."
    "ZGkv0zh8TLhkovO3AdaNmKSXKnii3L09R6_LG2pFa38"
)


def _close_info(exc: ConnectionClosed) -> str:
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is None:
        return "geschlossen ohne Close-Frame"
    return f"Close-Code {rcvd.code}, Grund {rcvd.reason!r}"


async def probe(label: str, token: str, erwartet: str) -> None:
    url = f"{BASE}?token={token}"
    try:
        async with connect(url) as ws:
            try:
                nachricht = await asyncio.wait_for(ws.recv(), timeout=5)
                ergebnis = f"Nachricht empfangen: {nachricht!r}"
            except ConnectionClosed as e:
                ergebnis = _close_info(e)
            except asyncio.TimeoutError:
                ergebnis = "Verbindung steht, Server schweigt (Timeout 5s)"
    except InvalidStatus as e:
        ergebnis = f"Handshake abgelehnt: HTTP {e.response.status_code}"
    except ConnectionClosed as e:
        ergebnis = _close_info(e)
    except OSError as e:
        ergebnis = f"kein Verbindungsaufbau: {e}"

    print(f"[{label}]")
    print(f"  erwartet: {erwartet}")
    print(f"  bekommen: {ergebnis}")
    print()


async def main() -> None:
    frisch = create_access_token({"user_id": str(USER_ID)})
    print(f"Ziel: {BASE}\n")

    await probe("1 kein Token", "", "Close-Code 1008")
    await probe("2 Muell", "keinjwt", "Close-Code 1008")
    await probe("3 abgelaufen", ABGELAUFEN, "Close-Code 1008")
    await probe(
        "4 gueltig",
        frisch,
        f"Verbindung steht, Server schweigt (Go loggt 'verbunden: user={USER_ID}')",
    )


asyncio.run(main())
