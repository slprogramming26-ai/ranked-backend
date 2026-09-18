"""Testet ratelimit.go: Nachrichten je Verbindung und Verbindungsversuche je IP.

Zwei getrennte Bremsen, die nichts miteinander zu tun haben:

  1. Nachrichten -- ein eigener Token-Bucket je SOCKET (5/s, Burst 10).
     Wer schneller sendet, bekommt kind='error' mit detail='rate limit exceeded'
     und bleibt verbunden.
  2. Verbindungen -- ein Token-Bucket je IP (10/s, Burst 30), geprueft VOR dem
     HTTP-Upgrade. Wer zu schnell verbindet, bekommt HTTP 429.

TRICK, der diesen Test billig macht: Go prueft das Nachrichten-Limit VOR
parseClientMessage. Eine formal kaputte Nachricht kostet also eine Marke, kommt
aber nie bei Python an. Fast alle Faelle hier schicken deshalb Muell -- das
Limit wird exakt so belastet wie von echten Nachrichten, aber es entsteht KEINE
einzige DB-Zeile. Nur Test 1 schickt drei echte DMs (Sender 2 -> Lama 14), damit
auch bewiesen ist, dass normales Tippen ungebremst durchgeht.

Unterscheidung der beiden Fehlerarten (beide sind kind='error'):
  detail == 'rate limit exceeded'  -> die Bremse hat gegriffen
  detail == "missing or unknown 'kind' ..."  -> Bremse durch, Formpruefung hat
                                                abgelehnt (= Marke war da)

Voraussetzungen:
  - uvicorn laeuft auf 127.0.0.1:8000  (OHNE --reload)
  - ws-gateway laeuft auf 127.0.0.1:8080 -- nach jeder Code-Aenderung neu bauen
    (`go build -o ws-gateway.exe .`), sonst testest du die alte Exe

Start:  venv\\Scripts\\python.exe scripts\\ws_gateway_ratelimit_test.py

Laufzeit ~25 s (die Pausen sind noetig, damit sich die Eimer nachfuellen).
"""

import asyncio
import http.client
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect

from app.oauth2 import create_access_token

HOST = "127.0.0.1"
PORT = 8080
BASE = f"ws://{HOST}:{PORT}/ws/chat"

SENDER = 2      # Sebastian
RECIPIENT = 14  # Lama (Testaccount)

# Muss zu ratelimit.go passen. Steht hier bewusst doppelt: laufen die Zahlen
# auseinander, soll dieser Test rot werden und nicht stillschweigend schweigen.
MSG_RATE = 5
MSG_BURST = 10
CONN_BURST = 30

MUELL = {"kind": "quatsch"}  # scheitert an der Formpruefung, nie an Python
FORMFEHLER = "missing or unknown 'kind' (expected 'dm' or 'group')"
GEBREMST = "rate limit exceeded"

# ------------------------------------------------------------------ Helfer ---
ERGEBNISSE: list[tuple[str, bool, str]] = []


def melde(label: str, ok: bool, text: str) -> None:
    ERGEBNISSE.append((label, ok, text))
    print(f"[{'OK    ' if ok else 'FEHLER'}] {label}: {text}")


async def sende_viele(ws, anzahl: int, timeout: float = 20.0) -> list[dict]:
    """Schickt `anzahl` Muell-Nachrichten am Stueck und holt ebenso viele Antworten.

    Erst alles raus, dann alles rein -- so entsteht die Gleichzeitigkeit, die
    ein einzeln-warten-einzeln-senden nie erzeugen wuerde.
    """
    for _ in range(anzahl):
        await ws.send(json.dumps(MUELL))

    antworten = []
    for _ in range(anzahl):
        try:
            roh = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        try:
            antworten.append(json.loads(roh))
        except json.JSONDecodeError:
            antworten.append({"kind": "__kein_json__", "detail": roh})
    return antworten


def zaehle(antworten: list[dict]) -> tuple[int, int, int]:
    """(durchgelassen, gebremst, unerwartet)"""
    durch = sum(1 for a in antworten if a.get("detail") == FORMFEHLER)
    gebremst = sum(1 for a in antworten if a.get("detail") == GEBREMST)
    return durch, gebremst, len(antworten) - durch - gebremst


def http_versuch() -> int:
    """Ein nackter GET auf /ws/chat. Gibt den Statuscode zurueck.

    Ohne WebSocket-Header lehnt Accept mit 426 'Upgrade Required' ab -- das ist
    hier das Zeichen fuer 'die IP-Bremse hat mich durchgelassen'. 429 heisst:
    abgewiesen, und zwar bevor ueberhaupt ein Upgrade versucht wurde. Die beiden
    Codes zu unterscheiden ist der ganze Punkt des Tests: nur so ist belegt,
    dass die Bremse VOR dem Upgrade sitzt und nicht dahinter.
    """
    verb = http.client.HTTPConnection(HOST, PORT, timeout=5)
    try:
        verb.request("GET", "/ws/chat")
        return verb.getresponse().status
    finally:
        verb.close()


# ------------------------------------------------------------------- Tests ---
async def main() -> None:
    token = create_access_token({"user_id": str(SENDER)})
    token_b = create_access_token({"user_id": str(RECIPIENT)})

    print(f"Ziel: {BASE}  (msg {MSG_RATE}/s burst {MSG_BURST}, conn burst {CONN_BURST})\n")

    async with connect(f"{BASE}?token={token}") as a:

        # --- 1: normales Tippen merkt nichts ------------------------------
        # Der wichtigste Fall der Reihe. Drei echte DMs schnell hintereinander,
        # so wie ein Mensch tippt. Kommt hier etwas anderes als 'ack', ist der
        # Limiter zu streng und stoert echte Nutzer.
        acks = 0
        for i in range(3):
            await a.send(json.dumps({
                "kind": "dm", "to": RECIPIENT,
                "message": f"ratelimit test 1 ({i + 1}/3)",
                "client_msg_id": str(uuid.uuid4()),
            }))
        for _ in range(3):
            antwort = json.loads(await asyncio.wait_for(a.recv(), timeout=20.0))
            if antwort.get("kind") == "ack":
                acks += 1
        melde("01 drei echte DMs schnell hintereinander", acks == 3,
              f"{acks}/3 ack" + ("" if acks == 3 else "  <- Limiter stoert echte Nutzer"))

        # --- 2: der volle Burst geht am Stueck durch -----------------------
        await asyncio.sleep(2.5)  # Eimer wieder voll (3 Marken brauchen 0,6 s)
        antworten = await sende_viele(a, MSG_BURST)
        durch, gebremst, seltsam = zaehle(antworten)
        melde(f"02 burst von {MSG_BURST} am Stueck", durch == MSG_BURST and gebremst == 0,
              f"{durch} durchgelassen, {gebremst} gebremst, {seltsam} unerwartet")

        # --- 3: Flut -------------------------------------------------------
        # 40 auf einen Schlag bei vollem Eimer: die ersten ~10 zahlen mit den
        # Burst-Marken, der Rest faellt in die Bremse. Genaue Zahlen sind
        # zeitabhaengig, deshalb grosszuegige Grenzen -- geprueft wird, DASS
        # gebremst wird und dass jede Nachricht eine Antwort bekommt (4.4).
        await asyncio.sleep(3.0)
        antworten = await sende_viele(a, 40)
        durch, gebremst, seltsam = zaehle(antworten)
        melde("03 flut von 40",
              len(antworten) == 40 and gebremst >= 20 and durch >= 5 and seltsam == 0,
              f"{len(antworten)}/40 beantwortet: {durch} durchgelassen, "
              f"{gebremst} gebremst, {seltsam} unerwartet")

        # --- 4: die Verbindung lebt weiter ---------------------------------
        # Deine Entscheidung war 'bremsen, nicht trennen'. Waere hier Schluss,
        # wuerde jeder gebremste Client sofort neu verbinden -- Reconnect-Sturm.
        await asyncio.sleep(3.0)
        antworten = await sende_viele(a, 1)
        durch, gebremst, _ = zaehle(antworten)
        melde("04 verbindung lebt, eimer hat sich erholt", durch == 1,
              "eine Nachricht nach der Flut wieder durchgelassen" if durch == 1
              else f"durch={durch}, gebremst={gebremst}")

        # --- 5: zweite Verbindung ist unbeeindruckt ------------------------
        # Beweist 'pro Verbindung, nicht global'. Waere der Eimer geteilt,
        # haette die Flut aus Test 3 auch b lahmgelegt.
        async with connect(f"{BASE}?token={token_b}") as b:
            flut = asyncio.create_task(sende_viele(a, 40))
            await asyncio.sleep(0.2)  # a steckt jetzt mitten in der Bremse
            antworten_b = await sende_viele(b, 3)
            durch_b, gebremst_b, _ = zaehle(antworten_b)
            await flut
            melde("05 zweiter socket bleibt unbeeindruckt", durch_b == 3,
                  f"b: {durch_b}/3 durchgelassen, {gebremst_b} gebremst")

    # --- 6: Verbindungsversuche je IP --------------------------------------
    # Nackte GETs auf /ws/chat. 400 = durchgelassen (Accept scheitert mangels
    # WS-Headern), 429 = von der IP-Bremse abgewiesen, vor dem Upgrade.
    # Laeuft zuletzt, weil es den IP-Eimer fuer ein paar Sekunden leerraeumt.
    await asyncio.sleep(1.0)
    codes = [http_versuch() for _ in range(CONN_BURST + 20)]
    durchgelassen = codes.count(426)
    zu_viele = codes.count(429)
    andere = len(codes) - durchgelassen - zu_viele
    erste_429 = codes.index(429) + 1 if 429 in codes else None
    melde("06 verbindungsflut wird vor dem upgrade abgewiesen",
          zu_viele >= 10 and durchgelassen >= 10 and andere == 0,
          f"{durchgelassen}x 426 durchgelassen, {zu_viele}x 429 abgewiesen, "
          f"{andere}x anderes; erste 429 beim Versuch {erste_429}")

    gruen = sum(1 for _, ok, _ in ERGEBNISSE if ok)
    print(f"\n{gruen}/{len(ERGEBNISSE)} gruen")
    if gruen != len(ERGEBNISSE):
        print("Fehlgeschlagen: " + ", ".join(l for l, ok, _ in ERGEBNISSE if not ok))
        sys.exit(1)


asyncio.run(main())
