"""Testet die Presence des Go-Gateways (PROTOCOL.md 7).

Baut eine echte WebSocket-Verbindung zum Gateway auf, haelt sie offen und
schaut dabei in Redis nach, was Go dort eingetragen hat:

  1. Nach dem Verbindungsaufbau steht ws:online:{user_id} mit der Instanz-Kennung.
  2. Die Restlaufzeit liegt bei 60 s (SETEX, nicht SET).
  3. Nach 27 s ist sie wieder oben -> die Erneuerungs-Goroutine laeuft.
  4. Nach dem Trennen ist der Eintrag sofort weg (DEL, nicht erst per TTL).

Start:  venv\\Scripts\\python.exe scripts\\ws_gateway_presence_test.py
        venv\\Scripts\\python.exe scripts\\ws_gateway_presence_test.py --schnell

--schnell laesst Test 3 aus und braucht dann keine 27 Sekunden.

Voraussetzung: ws-gateway laeuft auf 127.0.0.1:8080 gegen dasselbe Redis
wie dieses Backend (.env in beiden Repos).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect

from app.oauth2 import create_access_token
from app.redis_client import redis_client

BASE = "ws://127.0.0.1:8080/ws/chat"
USER_ID = 2
KEY = f"ws:online:{USER_ID}"

# Wartezeit fuer Test 3: knapp ueber dem 25-s-Takt der Goroutine.
REFRESH_WARTEN = 27

ergebnisse: list[bool] = []


def pruefe(label: str, ok: bool, detail: str) -> None:
    ergebnisse.append(ok)
    print(f"  [{'ok' if ok else 'FEHLER'}] {label}")
    print(f"         {detail}")


async def main(mit_refresh: bool) -> None:
    print(f"Ziel: {BASE}")
    print(f"Redis-Eintrag: {KEY}\n")

    # Vorbedingung: ein Rest aus einem frueheren Lauf wuerde Test 1 verfaelschen.
    if redis_client.delete(KEY):
        print("Hinweis: alter Eintrag lag noch da und wurde geloescht.\n")

    token = create_access_token({"user_id": str(USER_ID)})

    async with connect(f"{BASE}?token={token}") as ws:
        await asyncio.sleep(0.5)  # Go braucht einen Moment fuer das SETEX

        wert = redis_client.get(KEY)
        pruefe(
            "1 Eintrag existiert nach dem Verbindungsaufbau",
            wert is not None,
            f"wert={wert!r}  (das ist die InstanceID des Go-Prozesses)",
        )

        ttl = redis_client.ttl(KEY)
        pruefe(
            "2 Restlaufzeit liegt bei 60 s",
            50 <= ttl <= 60,
            f"ttl={ttl}s  (-1 hiesse: gesetzt OHNE Ablauf, also SET statt SETEX)",
        )

        if mit_refresh:
            print(f"\n  ... warte {REFRESH_WARTEN} s auf die Erneuerung\n")
            await asyncio.sleep(REFRESH_WARTEN)

            ttl_neu = redis_client.ttl(KEY)
            pruefe(
                "3 Restlaufzeit nach der Erneuerung wieder oben",
                ttl_neu >= 50,
                f"ttl={ttl_neu}s  (ohne Erneuerung waere er jetzt bei ~33s)",
            )
        else:
            print("  [--] 3 Erneuerung uebersprungen (--schnell)\n")

        await ws.close()

    await asyncio.sleep(0.5)  # Go braucht einen Moment fuer das DEL

    danach = redis_client.get(KEY)
    pruefe(
        "4 Eintrag ist nach dem Trennen geloescht",
        danach is None,
        f"wert={danach!r}  (nicht None hiesse: clearPresence lief nicht)",
    )

    bestanden = sum(ergebnisse)
    print(f"\n{bestanden}/{len(ergebnisse)} bestanden")


schnell = "--schnell" in sys.argv
asyncio.run(main(mit_refresh=not schnell))
sys.exit(0 if all(ergebnisse) else 1)
