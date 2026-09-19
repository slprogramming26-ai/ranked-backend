"""Rauchtest gegen den DEPLOYTEN ws-gateway (Railway) -- nur ueber den WebSocket.

Anders als die anderen ws_gateway_*-Skripte spaeht dieses NICHT in Redis und
liest NICHT aus der DB. Grund: das lokale REDIS_URL zeigt auf localhost, in
Produktion arbeitet ein anderes Redis. Alles, was hier geprueft wird, sieht das
Skript also genau so, wie der Flutter-Client es sehen wuerde.

Trotzdem beweist es die ganze Kette, weil Go Pythons Antwort WOERTLICH in den
Socket schiebt (PROTOCOL.md 5.1):

    Skript --WS--> Go --HTTPS--> FastAPI --> Supabase
    Skript <--WS-- Go <--200---- FastAPI (Body unveraendert)

Kommt ein 'ack' zurueck, hat die Nachricht die Datenbank erreicht.

Voraussetzung: das lokale SECRET_KEY ist dasselbe wie auf Railway -- dann
akzeptiert der deployte Gateway ein hier erzeugtes Token. Genau das prueft
Fall 2; scheitert er, sind die Schluessel verschieden.

Start:
    venv\\Scripts\\python.exe scripts\\ws_gateway_prod_smoke.py <gateway-adresse>

Beispiele fuer <gateway-adresse> (alle drei Schreibweisen gehen):
    ws-gateway-production.up.railway.app
    wss://ws-gateway-production.up.railway.app
    wss://ws-gateway-production.up.railway.app/ws/chat

Ohne Argument laeuft es gegen ws://127.0.0.1:8080/ws/chat.

Schreibt GENAU ZWEI echte Nachrichtenzeilen (Faelle 4 und 5, Sender 2 -> Lama 14).
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.oauth2 import create_access_token

SENDER = 2      # Sebastian
RECIPIENT = 14  # Lama (Testaccount)

MUELL = {"kind": "quatsch"}
FORMFEHLER = "missing or unknown 'kind' (expected 'dm' or 'group')"
GEBREMST = "rate limit exceeded"


def ziel(argv: list[str]) -> str:
    """Macht aus dem Argument eine vollstaendige WS-URL."""
    if len(argv) < 2:
        return "ws://127.0.0.1:8080/ws/chat"

    roh = argv[1].strip().rstrip("/")
    if roh.startswith("http://"):
        roh = "ws://" + roh[len("http://"):]
    elif roh.startswith("https://"):
        roh = "wss://" + roh[len("https://"):]
    elif not roh.startswith(("ws://", "wss://")):
        # Nackter Hostname -> in Produktion immer verschluesselt.
        roh = "wss://" + roh

    if not roh.endswith("/ws/chat"):
        roh += "/ws/chat"
    return roh


ERGEBNISSE: list[tuple[str, bool, str]] = []


def melde(label: str, ok: bool, text: str) -> None:
    ERGEBNISSE.append((label, ok, text))
    print(f"[{'OK    ' if ok else 'FEHLER'}] {label}: {text}")


async def antwort_auf(ws, payload: dict, timeout: float = 20.0) -> dict | None:
    await ws.send(json.dumps(payload))
    try:
        roh = await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    try:
        return json.loads(roh)
    except json.JSONDecodeError:
        return {"kind": "__kein_json__", "detail": roh}


async def main() -> None:
    url = ziel(sys.argv)
    token = create_access_token({"user_id": str(SENDER)})
    token_empfaenger = create_access_token({"user_id": str(RECIPIENT)})

    print(f"Ziel: {url}\n")

    # --- 1: Muell-Token wird abgewiesen --------------------------------
    # Beweist, dass der Gateway ueberhaupt erreichbar ist UND dass die
    # Tokenpruefung greift. Kein DB-Zugriff.
    try:
        async with connect(f"{url}?token=kaputt") as ws:
            await asyncio.wait_for(ws.recv(), timeout=10.0)
        melde("01 muell-token abgewiesen", False, "Verbindung blieb offen")
    except ConnectionClosed as e:
        ok = e.rcvd is not None and e.rcvd.code == 1008
        melde("01 muell-token abgewiesen", ok,
              f"Close {e.rcvd.code} {e.rcvd.reason!r}" if e.rcvd else "ohne Close-Frame")
    except Exception as e:
        melde("01 muell-token abgewiesen", False,
              f"{type(e).__name__}: {e}  <- Gateway nicht erreichbar?")
        print("\nAbbruch: ohne erreichbaren Gateway sind die restlichen Faelle sinnlos.")
        sys.exit(1)

    # --- 2: gueltiges Token wird angenommen -----------------------------
    # Der eigentliche Beweis, dass SECRET_KEY hier und auf Railway gleich ist.
    async with connect(f"{url}?token={token}") as sender:
        try:
            roh = await asyncio.wait_for(sender.recv(), timeout=2.0)
            melde("02 gueltiges token angenommen", False, f"unerwartete Nachricht: {roh!r}")
        except asyncio.TimeoutError:
            melde("02 gueltiges token angenommen", True,
                  "Verbindung steht, Server schweigt (SECRET_KEY stimmt ueberein)")
        except ConnectionClosed as e:
            grund = f"{e.rcvd.code} {e.rcvd.reason!r}" if e.rcvd else "ohne Close-Frame"
            melde("02 gueltiges token angenommen", False,
                  f"getrennt mit {grund}  <- SECRET_KEY hier und auf Railway verschieden?")
            sys.exit(1)

        # --- 3: Formfehler beantwortet Go selbst ------------------------
        # Kommt hier der Formfehlertext, hat Go geantwortet, ohne Python zu
        # fragen (PROTOCOL.md 5.2). Kein DB-Zugriff.
        a = await antwort_auf(sender, MUELL)
        ok = a is not None and a.get("kind") == "error" and a.get("detail") == FORMFEHLER
        melde("03 formfehler von go selbst", ok, json.dumps(a, ensure_ascii=False))

        # --- 4: echte DM -> Strecke B komplett ---------------------------
        # ack = Go hat an FastAPI geschickt, FastAPI hat nach Supabase
        # geschrieben und geantwortet, Go hat den Body durchgereicht.
        # delivered_live wird hier NICHT festgenagelt: Empfaenger 14 koennte
        # aus einem frueheren Lauf noch bis zu 60 s als online gelten.
        cmid = str(uuid.uuid4())
        a = await antwort_auf(sender, {
            "kind": "dm", "to": RECIPIENT,
            "message": "prod smoke test 4", "client_msg_id": cmid,
        })
        if a is None:
            melde("04 strecke B (dm -> ack)", False, "keine Antwort")
        elif a.get("kind") == "ack":
            melde("04 strecke B (dm -> ack)", True,
                  f"ack, delivered_live={a.get('delivered_live')} "
                  f"(Nachricht ist in der Datenbank)")
        elif a.get("detail") == "backend unavailable":
            melde("04 strecke B (dm -> ack)", False,
                  "'backend unavailable' -> BACKEND_URL falsch oder Backend nicht erreichbar")
        else:
            melde("04 strecke B (dm -> ack)", False, json.dumps(a, ensure_ascii=False))

        # --- 5 + 6: Empfaenger online -> Presence und Push ---------------
        async with connect(f"{url}?token={token_empfaenger}") as empfaenger:
            await asyncio.sleep(1.0)  # markOnline durch

            cmid2 = str(uuid.uuid4())
            a = await antwort_auf(sender, {
                "kind": "dm", "to": RECIPIENT,
                "message": "prod smoke test 5", "client_msg_id": cmid2,
            })
            ok = a is not None and a.get("kind") == "ack" and a.get("delivered_live") == 1
            melde("05 presence in produktion (delivered_live=1)", ok,
                  json.dumps(a, ensure_ascii=False) if a else "keine Antwort")

            # Strecke C: der Push geht ueber Railways Redis an den zweiten Socket.
            try:
                roh = await asyncio.wait_for(empfaenger.recv(), timeout=10.0)
                da = json.loads(roh).get("client_msg_id")
                melde("06 strecke C (push im empfaengersocket)", da == cmid2,
                      f"client_msg_id={da!r}" + ("" if da == cmid2 else f", erwartet {cmid2!r}"))
            except asyncio.TimeoutError:
                melde("06 strecke C (push im empfaengersocket)", False,
                      "nichts zugestellt -> Redis-Abo oder Registry pruefen")

        # --- 7: die Bremse greift auch in Produktion --------------------
        # Muell-Nachrichten: kosten eine Marke, erreichen Python aber nie.
        await asyncio.sleep(3.0)  # Eimer nachfuellen lassen
        for _ in range(40):
            await sender.send(json.dumps(MUELL))
        durch = gebremst = 0
        for _ in range(40):
            try:
                roh = await asyncio.wait_for(sender.recv(), timeout=20.0)
            except asyncio.TimeoutError:
                break
            d = json.loads(roh).get("detail")
            if d == GEBREMST:
                gebremst += 1
            elif d == FORMFEHLER:
                durch += 1
        melde("07 ratelimit greift", gebremst >= 20 and durch >= 5,
              f"{durch} durchgelassen, {gebremst} gebremst (von 40)")

    gruen = sum(1 for _, ok, _ in ERGEBNISSE if ok)
    print(f"\n{gruen}/{len(ERGEBNISSE)} gruen")
    if gruen != len(ERGEBNISSE):
        print("Fehlgeschlagen: " + ", ".join(l for l, ok, _ in ERGEBNISSE if not ok))
        sys.exit(1)


asyncio.run(main())
