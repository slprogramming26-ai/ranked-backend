"""Testet Strecke C end-to-end: Python -> Redis ws:push -> Go -> Empfaengersocket.

Strecke B hat gezeigt, dass der Umschlag bis Redis kommt. Hier geht es um das,
was Go DANACH macht (PROTOCOL.md 6):
  - genau ein Abo pro Prozess, Fanout an die Sockets der `targets`
  - payload geht WOERTLICH raus (json.RawMessage, nie umgebaut)
  - nur an die Ziele: Sender und Unbeteiligte bleiben still
  - Registry als MENGE: zwei Geraete bekommen beide den Push, und das Trennen
    des einen stellt den Nutzer weder offline noch aus der Registry
  - kaputte / fremde Umschlaege werden verworfen, das Abo lebt weiter

ACHTUNG: laeuft gegen die ECHTE Supabase-DB. Die Tests 01, 06, 08 und 10
schreiben je eine echte Nachrichtenzeile (Sender 2 -> Lama / Gruppe 2). Die
Tests 12-15 publizieren direkt auf Redis und beruehren die DB nicht.

Voraussetzungen:
  - uvicorn laeuft auf 127.0.0.1:8000  (OHNE --reload)
  - ws-gateway laeuft auf 127.0.0.1:8080 -- nach jeder Code-Aenderung neu bauen
    (`go build -o ws-gateway.exe .`), sonst testest du die alte Exe

Start:  venv\\Scripts\\python.exe scripts\\ws_gateway_strecke_c_test.py
"""

import asyncio
import json
import sys
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
GROUP = 2             # Mitglieder [2, 9]
GROUP_MEMBER = 9      # das andere Mitglied von Gruppe 2
GROUP_CURRENT_KV = 1

# Nach dem Verbinden kurz warten: markOnline geht nach Railway, erst DANACH
# steht der Socket in der Registry.
ANMELDEN = 0.8
# So lange muss ein Socket schweigen, damit "kam nichts" als bewiesen gilt.
STILLE = 1.5

# ------------------------------------------------------------------ Helfer ---
ERGEBNISSE: list[tuple[str, bool, str]] = []


def melde(label: str, ok: bool, text: str) -> None:
    ERGEBNISSE.append((label, ok, text))
    print(f"[{'OK    ' if ok else 'FEHLER'}] {label}: {text}")


def token(user_id: int) -> str:
    return create_access_token({"user_id": str(user_id)})


async def sende(ws, payload: dict, timeout: float = 15.0) -> dict | None:
    """Schickt eine Nachricht und wartet auf GENAU eine Antwort (PROTOCOL.md 4.4)."""
    await ws.send(json.dumps(payload))
    try:
        return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
    except asyncio.TimeoutError:
        return None


async def empfange(ws, timeout: float = 5.0) -> str | None:
    """Rohes Frame (str) oder None, wenn in `timeout` nichts kam."""
    try:
        return await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


def dm(text: str) -> tuple[dict, str]:
    cmid = str(uuid.uuid4())
    return {"kind": "dm", "to": RECIPIENT, "message": text, "client_msg_id": cmid}, cmid


def pruefe_push(label: str, roh: str | None, **felder) -> None:
    if roh is None:
        melde(label, False, "nichts zugestellt")
        return
    daten = json.loads(roh)
    abweichungen = [
        f"{name}={daten.get(name)!r} statt {wert!r}"
        for name, wert in felder.items()
        if daten.get(name) != wert
    ]
    if abweichungen:
        melde(label, False, "; ".join(abweichungen) + f"  (ganzer Push: {daten})")
    elif not daten.get("created_at"):
        melde(label, False, f"created_at fehlt  (ganzer Push: {daten})")
    else:
        melde(label, True, f"kind={daten.get('kind')!r}, created_at gesetzt")


async def pruefe_still(label: str, ws, grund: str) -> None:
    roh = await empfange(ws, timeout=STILLE)
    if roh is None:
        melde(label, True, grund)
    else:
        melde(label, False, f"unerwartete Zustellung: {roh!r}")


def publiziere_roh(umschlag: str) -> None:
    """Direkt auf ws:push, an Python vorbei -- testet nur Go."""
    redis_client.publish(PUSH_CHANNEL, umschlag)


# ------------------------------------------------------------------- Tests ---
async def dm_tests() -> None:
    async with connect(f"{BASE}?token={token(SENDER)}") as sender, \
               connect(f"{BASE}?token={token(RECIPIENT)}") as empfaenger, \
               connect(f"{BASE}?token={token(GROUP_MEMBER)}") as unbeteiligt:
        await asyncio.sleep(ANMELDEN)

        # --- 01-04: normale DM --------------------------------------------
        nachricht, cmid = dm("strecke-c test 01")
        antwort = await sende(sender, nachricht)
        if antwort and antwort.get("kind") == "ack" and antwort.get("delivered_live") == 1:
            melde("01 ack an Sender", True, json.dumps(antwort))
        else:
            melde("01 ack an Sender", False, f"Antwort: {antwort}")

        pruefe_push("02 Empfaenger bekommt die DM", await empfange(empfaenger),
                    kind="dm", sender_id=SENDER, message="strecke-c test 01",
                    client_msg_id=cmid)

        # Der Sender bekommt NUR das ack, keinen Push seiner eigenen Nachricht.
        await pruefe_still("03 Sender bekommt keinen Push", sender, "nur das ack, wie erwartet")
        await pruefe_still("04 Unbeteiligter (user 9) bleibt still", unbeteiligt,
                           "targets wirken, kein Broadcast an alle")

        # --- 05: Duplikat -> Python pusht nicht nochmal -------------------
        await sende(sender, nachricht)
        await pruefe_still("05 gleiche client_msg_id -> kein zweiter Push", empfaenger,
                           "Duplikatschutz wirkt bis in den Socket")


async def zwei_geraete_tests() -> None:
    """Die Entscheidung 'Menge von Sockets pro Nutzer' (registry.go) und der
    remove()-Rueckgabewert aus ws.go (3a)."""
    async with connect(f"{BASE}?token={token(SENDER)}") as sender:
        handy = await connect(f"{BASE}?token={token(RECIPIENT)}")
        tablet = await connect(f"{BASE}?token={token(RECIPIENT)}")
        await asyncio.sleep(ANMELDEN)

        # --- 06: beide Geraete bekommen den Push --------------------------
        nachricht, cmid = dm("strecke-c test 06 (zwei geraete)")
        await sende(sender, nachricht)
        roh_handy, roh_tablet = await asyncio.gather(empfange(handy), empfange(tablet))
        if roh_handy and roh_tablet and roh_handy == roh_tablet \
                and json.loads(roh_handy).get("client_msg_id") == cmid:
            melde("06 beide Geraete bekommen den Push", True, "byte-gleich auf beiden Sockets")
        else:
            melde("06 beide Geraete bekommen den Push", False,
                  f"handy={roh_handy!r}, tablet={roh_tablet!r}")

        # --- 07: Tablet weg -> Nutzer bleibt online -----------------------
        # Das ist der 3a-Fix: remove() gibt false zurueck, kein DEL.
        await tablet.close()
        await asyncio.sleep(0.5)
        if redis_client.exists(f"ws:online:{RECIPIENT}"):
            melde("07 Tablet getrennt -> weiter online", True, "ws:online:14 steht noch")
        else:
            melde("07 Tablet getrennt -> weiter online", False,
                  "Presence geloescht, obwohl das Handy noch verbunden ist")

        # --- 08: und das Handy bekommt weiter Pushes ----------------------
        # Der Reconnect-Bug aus Python (manager.py): dort haette das Abmelden der
        # einen Verbindung den Eintrag der anderen mit geloescht.
        nachricht, cmid = dm("strecke-c test 08 (nur noch handy)")
        antwort = await sende(sender, nachricht)
        roh = await empfange(handy)
        if roh and json.loads(roh).get("client_msg_id") == cmid \
                and antwort and antwort.get("delivered_live") == 1:
            melde("08 Handy bekommt weiter Pushes", True, "Push da, delivered_live=1")
        else:
            melde("08 Handy bekommt weiter Pushes", False, f"ack={antwort}, push={roh!r}")

        # --- 09: letztes Geraet weg -> sofort offline ---------------------
        await handy.close()
        await asyncio.sleep(0.5)
        if redis_client.exists(f"ws:online:{RECIPIENT}"):
            melde("09 letztes Geraet getrennt -> offline", False,
                  "ws:online:14 steht noch (erst TTL-Ablauf wuerde aufraeumen)")
        else:
            melde("09 letztes Geraet getrennt -> offline", True, "ws:online:14 sofort weg")


async def gruppen_test() -> None:
    async with connect(f"{BASE}?token={token(SENDER)}") as sender, \
               connect(f"{BASE}?token={token(GROUP_MEMBER)}") as mitglied:
        await asyncio.sleep(ANMELDEN)

        cmid = str(uuid.uuid4())
        antwort = await sende(sender, {
            "kind": "group", "to": GROUP, "message": "strecke-c test 10 (gruppe)",
            "key_version": GROUP_CURRENT_KV, "client_msg_id": cmid,
        })
        if not antwort or antwort.get("kind") != "ack":
            melde("10 Gruppenmitglied bekommt die Nachricht", False, f"kein ack: {antwort}")
            return
        pruefe_push("10 Gruppenmitglied bekommt die Nachricht", await empfange(mitglied),
                    group_chat_id=GROUP, sender_id=SENDER, key_version=GROUP_CURRENT_KV,
                    client_msg_id=cmid)
        await pruefe_still("11 Sender bekommt keinen Gruppen-Push", sender,
                           "Sender ist aus recipient_ids ausgeschlossen")


async def umschlag_tests() -> None:
    """An Python vorbei direkt auf Redis. Keine DB-Zeilen."""
    async with connect(f"{BASE}?token={token(RECIPIENT)}") as empfaenger:
        await asyncio.sleep(ANMELDEN)

        # --- 12: payload wird WOERTLICH durchgereicht ---------------------
        # Absichtlich 'haessliches' JSON: Leerzeichen, 1.50 statt 1.5, \u-Escape,
        # unbekanntes kind. Wuerde Go parsen und neu serialisieren, sähe das
        # Ergebnis anders aus.
        payload = '{"kind": "test_roh",  "zahl": 1.50, "text":"\\u00fc"}'
        publiziere_roh(
            '{"protocol_version": %d, "targets": [%d], "payload": %s}'
            % (PROTOCOL_VERSION, RECIPIENT, payload)
        )
        roh = await empfange(empfaenger)
        if roh == payload:
            melde("12 payload byte-gleich", True, f"{roh}")
        else:
            melde("12 payload byte-gleich", False, f"gesendet {payload!r}, bekommen {roh!r}")

        # --- 13: falsche protocol_version -> verworfen --------------------
        publiziere_roh(json.dumps({
            "protocol_version": PROTOCOL_VERSION + 1,
            "targets": [RECIPIENT], "payload": {"kind": "darf_nicht_ankommen"},
        }))
        await pruefe_still("13 fremde protocol_version verworfen", empfaenger,
                           "nicht zugestellt (Go-Log: 'push verworfen')")

        # --- 14: kaputter Umschlag -> verworfen, Abo lebt weiter ----------
        publiziere_roh("das ist kein json")
        await pruefe_still("14 kaputter Umschlag verworfen", empfaenger, "nicht zugestellt")

        # --- 15: unbekanntes Feld im Umschlag wird toleriert --------------
        # Python ist vertraut -> nachsichtig (push.go ohne DisallowUnknownFields).
        # Das beweist zugleich, dass das Abo nach 13/14 noch lebt.
        publiziere_roh(json.dumps({
            "protocol_version": PROTOCOL_VERSION, "targets": [RECIPIENT],
            "neues_feld_aus_zukunft": True, "payload": {"kind": "nach_muell"},
        }))
        roh = await empfange(empfaenger)
        if roh and json.loads(roh).get("kind") == "nach_muell":
            melde("15 unbekanntes Umschlagfeld toleriert, Abo lebt", True, roh)
        else:
            melde("15 unbekanntes Umschlagfeld toleriert, Abo lebt", False, f"bekommen {roh!r}")


async def main() -> None:
    # Presence-Reste aus frueheren Laeufen leben bis zu 60 s weiter.
    redis_client.delete(f"ws:online:{RECIPIENT}", f"ws:online:{GROUP_MEMBER}")

    print(f"Ziel: {BASE}  (sender={SENDER}, empfaenger={RECIPIENT}, gruppe={GROUP})\n")

    await dm_tests()
    await zwei_geraete_tests()
    await gruppen_test()
    await umschlag_tests()

    gruen = sum(1 for _, ok, _ in ERGEBNISSE if ok)
    print(f"\n{gruen}/{len(ERGEBNISSE)} gruen")
    if gruen != len(ERGEBNISSE):
        print("Fehlgeschlagen: " + ", ".join(l for l, ok, _ in ERGEBNISSE if not ok))
        sys.exit(1)


asyncio.run(main())
