"""Testet die Formpruefung des Go-Gateways (Strecke A, PROTOCOL.md 5.2).

Schickt ueber EINE Verbindung eine Reihe gueltiger und ungueltiger Nachrichten
und prueft, was Go zurueckschickt:

  - Formfehler -> {"kind": "error", "detail": "..."}, Go fragt Python gar nicht
                  erst (PROTOCOL.md 5.2). Verbindung bleibt offen.
  - Form ok    -> Go reicht an Python durch und schreibt DESSEN Antwort zurueck.
                  Hier interessiert nur, DASS die Form akzeptiert wurde; was
                  fachlich herauskommt, prueft ws_gateway_strecke_b_test.py.

ACHTUNG seit Strecke B: die gueltigen Faelle (12, 13, 15) legen echte
Nachrichtenzeilen in der Supabase-DB an -- zwei davon mit 4096 Zeichen.

Dass alles ueber EINE Verbindung laeuft, ist Absicht: damit prueft Test 14
nebenbei, dass ein Formfehler den Client nicht rauswirft (das `continue`).
Test 15 macht eine eigene Verbindung auf, weil er sie absichtlich toetet.

Start:  venv\\Scripts\\python.exe scripts\\ws_gateway_form_test.py
Voraussetzung: ws-gateway laeuft auf 127.0.0.1:8080 -- nach jeder Code-Aenderung
neu bauen (`go build -o ws-gateway.exe .`), sonst testest du die alte Exe.
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

BASE = "ws://127.0.0.1:8080/ws/chat"
USER_ID = 2

# Ein Emoji ist EIN Zeichen, aber 4 Bytes in UTF-8 -- und json.dumps escapt es
# zu "😀", also 12 Bytes. Genau daran haengt der Unterschied zwischen
# utf8.RuneCountInString und len() in Go.
EMOJI = "\U0001F600"

ERGEBNISSE: list[tuple[str, bool, str]] = []


def _melde(label: str, ok: bool, text: str) -> None:
    ERGEBNISSE.append((label, ok, text))
    print(f"[{'OK    ' if ok else 'FEHLER'}] {label}: {text}")


async def check(ws, label: str, payload, erwartet: str) -> bool:
    """`erwartet` sagt, was zurueckkommen muss:

      "kind:ack"  -> Form akzeptiert, Go hat durchgereicht; Pythons Antwort
                     muss dieses kind haben.
      sonst       -> Formfehler von Go: kind=error, dessen detail den Text
                     enthaelt. Python wird dabei GAR NICHT gefragt.

    Rueckgabe: False, wenn die Verbindung dabei gestorben ist (dann abbrechen).
    """
    durchgereicht = erwartet.startswith("kind:")
    roh = payload if isinstance(payload, str) else json.dumps(payload)
    try:
        await ws.send(roh)
        # Durchgereichte Faelle gehen ueber Python bis zur DB in Frankfurt,
        # Formfehler beantwortet Go sofort aus dem Speicher.
        antwort = await asyncio.wait_for(ws.recv(), timeout=15.0 if durchgereicht else 5.0)
    except asyncio.TimeoutError:
        antwort = None
    except ConnectionClosed as e:
        code = e.rcvd.code if e.rcvd else "?"
        _melde(label, False, f"Verbindung unerwartet geschlossen (Code {code})")
        return False

    if antwort is None:
        _melde(label, False, f"keine Antwort, erwartet war {erwartet!r} "
                             "(PROTOCOL.md 4.4: auf jede Nachricht genau eine)")
        return True

    if durchgereicht:
        try:
            daten = json.loads(antwort)
        except json.JSONDecodeError:
            _melde(label, False, f"Antwort ist kein JSON: {antwort!r}")
            return True
        soll = erwartet.split(":", 1)[1]
        if daten.get("kind") != soll:
            _melde(label, False, f"kind ist {daten.get('kind')!r}, erwartet {soll!r} "
                                 f"(ganze Antwort: {daten})")
        else:
            _melde(label, True, f"Form akzeptiert, durchgereicht -> {antwort}")
        return True

    try:
        daten = json.loads(antwort)
    except json.JSONDecodeError:
        _melde(label, False, f"Antwort ist kein JSON: {antwort!r}")
        return True

    if daten.get("kind") != "error":
        _melde(label, False, f"kind ist {daten.get('kind')!r}, erwartet 'error'")
    elif erwartet not in daten.get("detail", ""):
        _melde(label, False, f"detail {daten.get('detail')!r} enthaelt nicht {erwartet!r}")
    else:
        _melde(label, True, f"error mit detail {daten['detail']!r}")
    return True


async def formtests(token: str) -> None:
    async with connect(f"{BASE}?token={token}") as ws:
        faelle = [
            ("01 kein JSON", "das ist kein json", "invalid json"),
            ("02 unbekanntes Feld",
             {"kind": "dm", "to": 14, "message": "hi", "foo": 1}, "unknown field"),
            ("03 kind falsch",
             {"kind": "direct", "to": 14, "message": "hi"}, "missing or unknown 'kind'"),
            ("04 kind fehlt", {"to": 14, "message": "hi"}, "missing or unknown 'kind'"),
            ("05 to fehlt", {"kind": "dm", "message": "hi"}, "field 'to' is required"),
            ("06 message leer",
             {"kind": "dm", "to": 14, "message": ""}, "must be 1..4096 characters"),
            ("07 message 4097 Zeichen",
             {"kind": "dm", "to": 14, "message": "x" * 4097}, "must be 1..4096 characters"),
            ("08 key_version in dm",
             {"kind": "dm", "to": 14, "message": "hi", "key_version": 1},
             "not allowed for kind 'dm'"),
            ("09 key_version fehlt bei group",
             {"kind": "group", "to": 2, "message": "hi"}, "required for kind 'group'"),
            ("10 zwei JSON-Objekte in einem Frame",
             '{"kind":"dm","to":14,"message":"a"}{"kind":"dm","to":14,"message":"b"}',
             "expected exactly one json object"),
            ("11 4097 Emoji (Zeichen, nicht Bytes)",
             {"kind": "dm", "to": 14, "message": EMOJI * 4097}, "must be 1..4096 characters"),
            # --- ab hier muss Go die Form akzeptieren und durchreichen ---
            ("12 message genau 4096",
             {"kind": "dm", "to": 14, "message": "x" * 4096}, "kind:ack"),
            ("13 4096 Emoji (~49 KB auf der Leitung)",
             {"kind": "dm", "to": 14, "message": EMOJI * 4096}, "kind:ack"),
            # key_version 0 ist FORMAL gueltig (der *int-Zeiger in protocol.go
            # unterscheidet "fehlt" von "ist 0"). Dass Python es danach als
            # veraltet ablehnt, ist Fachlogik und beweist genau das Durchreichen.
            ("14 key_version 0 bei group ist formal gueltig",
             {"kind": "group", "to": 2, "message": "hi", "key_version": 0},
             "kind:key_outdated"),
            ("15 gueltige dm nach 14 Faellen (Verbindung lebt noch)",
             {"kind": "dm", "to": 14, "message": "hallo",
              "client_msg_id": str(uuid.uuid4())}, "kind:ack"),
        ]

        for label, payload, erwartet in faelle:
            if not await check(ws, label, payload, erwartet):
                print("\n  -> Abbruch, Verbindung ist weg.")
                return
            # Seit ratelimit.go (2026-09-18) noetig: 15 Faelle ohne Pause ueber
            # EINE Verbindung leeren den Eimer (5/s, Burst 10), ab Fall 11 kam
            # sonst 'rate limit exceeded' statt des erwarteten Formfehlers.
            # Etwas ueber 1/msgRate = 200 ms, damit jede Marke nachgewachsen ist.
            # Die Bremse selbst prueft ws_gateway_ratelimit_test.py.
            await asyncio.sleep(0.25)


async def readlimit_test(token: str) -> None:
    """Eigene Verbindung: 70 KB sprengen conn.SetReadLimit(64 KiB).
    Die Bibliothek schliesst dann selbst mit 1009 -- es gibt KEIN error-JSON,
    der Verstoss ist zu gross, um noch hoeflich zu antworten.
    """
    label = "16 ueber ReadLimit (70 KB)"
    try:
        async with connect(f"{BASE}?token={token}") as ws:
            await ws.send(json.dumps({"kind": "dm", "to": 14, "message": "x" * 70000}))
            antwort = await asyncio.wait_for(ws.recv(), timeout=5.0)
            _melde(label, False, f"unerwartete Antwort statt Close 1009: {antwort!r}")
    except ConnectionClosed as e:
        code = e.rcvd.code if e.rcvd else None
        if code == 1009:
            _melde(label, True, "Close-Code 1009 (message too big)")
        else:
            _melde(label, False, f"Close-Code {code}, erwartet 1009")
    except asyncio.TimeoutError:
        _melde(label, False, "keine Reaktion, erwartet Close 1009")


async def main() -> None:
    token = create_access_token({"user_id": str(USER_ID)})
    print(f"Ziel: {BASE}  (user_id={USER_ID})\n")

    await formtests(token)
    await readlimit_test(token)

    gruen = sum(1 for _, ok, _ in ERGEBNISSE if ok)
    print(f"\n{gruen}/{len(ERGEBNISSE)} gruen")
    if gruen != len(ERGEBNISSE):
        print("Fehlgeschlagen: " + ", ".join(l for l, ok, _ in ERGEBNISSE if not ok))
        sys.exit(1)


asyncio.run(main())
