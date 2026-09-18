"""Testet Strecke B end-to-end: Client -> Go -> Python -> DB/Redis -> Ack zurueck.

Die Form haben wir schon geprueft (ws_gateway_form_test.py). Hier geht es um das,
was DANACH passiert: Go schickt die formgepruefte Nachricht per HTTP an Python und
schreibt dessen Antwort WOERTLICH in den Socket des Senders (PROTOCOL.md 5.1).

Geprueft wird vor allem, dass Go ein dummes Rohr bleibt:
  - ack, key_outdated und error kommen unveraendert beim Client an
  - delivered_live stimmt, obwohl Python die Sockets nicht mehr kennt (Presence)
  - der Duplikat-Schutz wirkt durch das Rohr hindurch

ACHTUNG: laeuft gegen die ECHTE Supabase-DB. Die Tests 1, 2 und 5 schreiben je
eine echte Nachrichtenzeile (Sender 2 -> Lama/Gruppe 2). Test 3 und 7 schreiben
nichts, Test 6 auch nicht (der Tuersteher wirft vor dem Speichern).

Voraussetzungen:
  - uvicorn laeuft auf 127.0.0.1:8000  (OHNE --reload)
  - ws-gateway laeuft auf 127.0.0.1:8080 -- nach jeder Code-Aenderung neu bauen
    (`go build -o ws-gateway.exe .`), sonst testest du die alte Exe

Start:  venv\\Scripts\\python.exe scripts\\ws_gateway_strecke_b_test.py
"""

import asyncio
import json
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect

from app.oauth2 import create_access_token
from app.redis_client import redis_client
from app.ws.publisher import PROTOCOL_VERSION, PUSH_CHANNEL

BASE = "ws://127.0.0.1:8080/ws/chat"

SENDER = 2            # Sebastian
RECIPIENT = 14        # Lama (Testaccount)
GROUP = 2             # die einzige Gruppe mit sauberer Epoche
GROUP_CURRENT_KV = 1  # deren aktuelle key_version
NIEMAND = 999999      # existiert nicht -> FK-Verletzung -> fachlicher Fehler

# ---------------------------------------------------------------- Lauscher ---
# Test 4 prueft den Umschlag direkt in Redis (unabhaengig von Strecke C, die
# ws_gateway_strecke_c_test.py abdeckt). Dafuer lauschen wir selbst auf
# ws:push -- wie scripts/ws_internal_test.py, nur hier im Thread.
_envelopes: list[dict] = []
_lock = threading.Lock()


def _listen() -> None:
    sub = redis_client.pubsub()
    sub.subscribe(PUSH_CHANNEL)
    for msg in sub.listen():
        if msg["type"] != "message":
            continue
        with _lock:
            _envelopes.append(json.loads(msg["data"]))


def envelope_for(client_msg_id: str, wartezeit: float = 3.0) -> dict | None:
    ende = time.time() + wartezeit
    while time.time() < ende:
        with _lock:
            for env in _envelopes:
                if env.get("payload", {}).get("client_msg_id") == client_msg_id:
                    return env
        time.sleep(0.05)
    return None


# ------------------------------------------------------------------ Helfer ---
ERGEBNISSE: list[tuple[str, bool, str]] = []


def melde(label: str, ok: bool, text: str) -> None:
    ERGEBNISSE.append((label, ok, text))
    print(f"[{'OK    ' if ok else 'FEHLER'}] {label}: {text}")


async def sende(ws, payload: dict, timeout: float = 15.0) -> dict | None:
    """Schickt eine Nachricht und wartet auf GENAU eine Antwort (PROTOCOL.md 4.4)."""
    await ws.send(json.dumps(payload))
    try:
        roh = await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    try:
        return json.loads(roh)
    except json.JSONDecodeError:
        return {"__kein_json__": roh}


def erwarte(label: str, antwort: dict | None, **felder) -> None:
    """Prueft, dass die Antwort da ist und alle genannten Felder exakt passen."""
    if antwort is None:
        melde(label, False, "keine Antwort (PROTOCOL.md 4.4 verletzt)")
        return
    if "__kein_json__" in antwort:
        melde(label, False, f"kein JSON: {antwort['__kein_json__']!r}")
        return

    abweichungen = [
        f"{name}={antwort.get(name)!r} statt {wert!r}"
        for name, wert in felder.items()
        if antwort.get(name) != wert
    ]
    if abweichungen:
        melde(label, False, "; ".join(abweichungen) + f"  (ganze Antwort: {antwort})")
    else:
        melde(label, True, json.dumps(antwort, ensure_ascii=False))


# ------------------------------------------------------------------- Tests ---
async def main() -> None:
    threading.Thread(target=_listen, daemon=True).start()
    time.sleep(0.3)  # Abo steht, bevor die erste Nachricht laeuft

    token_sender = create_access_token({"user_id": str(SENDER)})
    token_empfaenger = create_access_token({"user_id": str(RECIPIENT)})

    # Aufraeumen: ein Presence-Eintrag aus einem frueheren Lauf lebt bis zu 60 s
    # weiter und wuerde Test 1 falsch gruen machen.
    redis_client.delete(f"ws:online:{RECIPIENT}")

    print(f"Ziel: {BASE}  (sender={SENDER}, empfaenger={RECIPIENT}, gruppe={GROUP})\n")

    async with connect(f"{BASE}?token={token_sender}") as sender:

        # --- 1: Empfaenger ist OFFLINE -------------------------------------
        cmid1 = str(uuid.uuid4())
        antwort = await sende(sender, {
            "kind": "dm", "to": RECIPIENT,
            "message": "strecke-b test 1 (offline)", "client_msg_id": cmid1,
        })
        erwarte("01 dm an offline Empfaenger", antwort,
                kind="ack", to=RECIPIENT, delivered_live=0)

        # --- 2: Empfaenger ist ONLINE --------------------------------------
        # Zweite Verbindung als user 14. Go setzt ws:online:14, Python liest es
        # per MGET -- genau der Trick, der delivered_live gerettet hat.
        async with connect(f"{BASE}?token={token_empfaenger}") as empfaenger:
            await asyncio.sleep(0.5)  # markOnline durch

            cmid2 = str(uuid.uuid4())
            antwort = await sende(sender, {
                "kind": "dm", "to": RECIPIENT,
                "message": "strecke-b test 2 (online)", "client_msg_id": cmid2,
            })
            erwarte("02 dm an online Empfaenger", antwort,
                    kind="ack", to=RECIPIENT, delivered_live=1)

            # --- 3: Duplikat, exakt dieselbe client_msg_id ------------------
            # Der Unique-Index schlaegt zu, Python gibt is_new=False zurueck und
            # pusht NICHT erneut -> delivered_live 0, obwohl 14 weiter online ist.
            antwort = await sende(sender, {
                "kind": "dm", "to": RECIPIENT,
                "message": "strecke-b test 2 (online)", "client_msg_id": cmid2,
            })
            erwarte("03 gleiche client_msg_id nochmal", antwort,
                    kind="ack", to=RECIPIENT, delivered_live=0)

            # --- 4: kam der Push bis Redis? --------------------------------
            env = envelope_for(cmid2)
            if env is None:
                melde("04 push auf ws:push", False, "kein Umschlag mit dieser client_msg_id")
            elif env.get("targets") != [RECIPIENT]:
                melde("04 push auf ws:push", False, f"targets={env.get('targets')!r}")
            elif env.get("protocol_version") != PROTOCOL_VERSION:
                melde("04 push auf ws:push", False, f"protocol_version={env.get('protocol_version')!r}")
            elif env.get("payload", {}).get("kind") != "dm":
                melde("04 push auf ws:push", False, f"payload.kind={env.get('payload', {}).get('kind')!r}")
            else:
                melde("04 push auf ws:push", True,
                      f"targets={env['targets']}, payload.kind='dm', created_at gesetzt")

            # --- 5: Strecke C: der Push kommt im Empfaengersocket an ---------
            # Umgedreht am 2026-09-17: bis dahin musste der Socket still bleiben.
            # Genau EIN Push (der aus Test 2) -- das Duplikat aus Test 3 darf
            # keinen zweiten ausloesen. Details: ws_gateway_strecke_c_test.py
            try:
                roh = await asyncio.wait_for(empfaenger.recv(), timeout=5.0)
                cmid_da = json.loads(roh).get("client_msg_id")
                if cmid_da != cmid2:
                    melde("05 push im empfaengersocket (Strecke C)", False,
                          f"falsche client_msg_id: {cmid_da!r}")
                else:
                    try:
                        zweiter = await asyncio.wait_for(empfaenger.recv(), timeout=1.5)
                        melde("05 push im empfaengersocket (Strecke C)", False,
                              f"zweiter Push (Duplikat?): {zweiter!r}")
                    except asyncio.TimeoutError:
                        melde("05 push im empfaengersocket (Strecke C)", True,
                              "genau ein Push, client_msg_id passt")
            except asyncio.TimeoutError:
                melde("05 push im empfaengersocket (Strecke C)", False,
                      "nichts zugestellt")

        # --- 6: Gruppe mit aktueller key_version ---------------------------
        cmid6 = str(uuid.uuid4())
        antwort = await sende(sender, {
            "kind": "group", "to": GROUP, "message": "strecke-b test 6 (gruppe)",
            "key_version": GROUP_CURRENT_KV, "client_msg_id": cmid6,
        })
        erwarte("06 group mit key_version 1", antwort, kind="ack", to=GROUP)

        # --- 7: veraltete key_version --------------------------------------
        # Der wichtigste Test der Reihe: key_outdated ist KEIN error. Kaeme hier
        # kind='error' an, koennte der Client nie wieder in die Gruppe schreiben.
        antwort = await sende(sender, {
            "kind": "group", "to": GROUP, "message": "strecke-b test 7 (veraltet)",
            "key_version": 99, "client_msg_id": str(uuid.uuid4()),
        })
        erwarte("07 group mit key_version 99", antwort,
                kind="key_outdated", group_chat_id=GROUP,
                current_version=GROUP_CURRENT_KV)

        # --- 8: fachlicher Fehler ------------------------------------------
        # Empfaenger existiert nicht -> FK-Verletzung -> ChatError -> HTTP 200
        # mit kind='error'. Go darf daraus KEIN 'backend unavailable' machen.
        antwort = await sende(sender, {
            "kind": "dm", "to": NIEMAND, "message": "strecke-b test 8 (niemand)",
            "client_msg_id": str(uuid.uuid4()),
        })
        if antwort is None:
            melde("08 dm an nicht existierenden Nutzer", False, "keine Antwort")
        elif antwort.get("kind") != "error":
            melde("08 dm an nicht existierenden Nutzer", False,
                  f"kind={antwort.get('kind')!r}, erwartet 'error'")
        elif antwort.get("detail") == "backend unavailable":
            melde("08 dm an nicht existierenden Nutzer", False,
                  "Go hat eine Stoerung gemeldet statt Pythons fachlichen Fehler "
                  "durchzureichen -- vermutlich kam kein HTTP 200")
        else:
            melde("08 dm an nicht existierenden Nutzer", True,
                  f"error mit detail {antwort.get('detail')!r}")

        # --- 9: Verbindung lebt nach alldem noch ---------------------------
        antwort = await sende(sender, {
            "kind": "dm", "to": RECIPIENT, "message": "strecke-b test 9 (lebt noch)",
            "client_msg_id": str(uuid.uuid4()),
        })
        erwarte("09 verbindung lebt nach 8 Faellen", antwort, kind="ack", to=RECIPIENT)

    gruen = sum(1 for _, ok, _ in ERGEBNISSE if ok)
    print(f"\n{gruen}/{len(ERGEBNISSE)} gruen")
    if gruen != len(ERGEBNISSE):
        print("Fehlgeschlagen: " + ", ".join(l for l, ok, _ in ERGEBNISSE if not ok))
        sys.exit(1)


asyncio.run(main())
