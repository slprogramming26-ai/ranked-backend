"""Testet client_msg_id + retryable in allen Antworten an den Sender (PROTOCOL.md 4.3.1/4.3.2).

Hintergrund: Das Frontend baut eine Outbox (wartend -> gesendet / fehlgeschlagen).
Dafuer muss JEDE Antwort die Sendungsnummer (client_msg_id) der Nachricht tragen,
auf die sie antwortet, und jeder error muss sagen, ob ein Retry Sinn hat.

Geprueft wird jede Antwortart einmal:
  01  ack                             -> client_msg_id passt
  02  Duplikat (gleiche ID nochmal)   -> wieder ack, gleiche ID, delivered_live 0
  03  Python-error (DM an Niemand)    -> retryable false, ID passt
  04  Python-error (Gruppe fehlt)     -> retryable false, ID passt
  05  key_outdated                    -> ID passt, KEIN retryable-Feld (ist ein Auftrag)
  06  Go-Formfehler, kaputtes JSON    -> retryable false, client_msg_id FEHLT
  07  Go-Formfehler, JSON lesbar      -> retryable false, client_msg_id FEHLT trotzdem
                                         (parseClientMessage gibt bei Fehler nil zurueck)
  08  Flut -> Go-Ratelimit            -> jede Antwort traegt die ID IHRER Nachricht;
                                         gebremst = retryable true, durchgelassen
                                         (Python: Gruppe fehlt) = retryable false
  09  backend unavailable             -> retryable true, ID passt
  10  Verbindung lebt danach weiter   -> nochmal backend unavailable mit neuer ID

Das Skript startet ZWEI eigene Gateway-Instanzen (dein Gateway auf 8080 bleibt
unberuehrt):
  8081 -> normale Instanz, redet mit uvicorn auf 8000
  8082 -> BACKEND_URL zeigt ins Leere (Port 1) -> jede gueltige Nachricht endet
          in 'backend unavailable'. So ist Test 9 ohne Python-Stoppen machbar.
Gesetzte Umgebungsvariablen gewinnen gegen die .env (config.go, loadDotEnv).

ACHTUNG: laeuft gegen die ECHTE Supabase-DB. Nur Test 1 schreibt eine echte
Nachrichtenzeile (Sender 2 -> Lama 14); Test 2 ist dasselbe Duplikat. Alle
anderen Faelle scheitern VOR dem Speichern (FK, Gruppe fehlt, key_version,
Form, Ratelimit) oder erreichen Python gar nicht (8082).

Voraussetzungen:
  - uvicorn laeuft auf 127.0.0.1:8000 (OHNE --reload) MIT dem neuen internals.py
  - ws-gateway.exe ist frisch gebaut (`go build` im ws-gateway-Ordner);
    das Skript prueft das am Datei-Datum und bricht sonst ab
  - Ports 8081 und 8082 sind frei

Start:  venv\\Scripts\\python.exe scripts\\ws_outbox_client_msg_id_test.py
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect

from app.oauth2 import create_access_token

GATEWAY_DIR = Path(r"C:\Users\Sebastian\Documents\Programmieren\Projekte\ranked\ws-gateway")
EXE = GATEWAY_DIR / "ws-gateway.exe"

PORT_OK = 8081
PORT_TOT = 8082
TOTES_BACKEND = "http://127.0.0.1:1"  # Port 1: dort lauscht nichts

SENDER = 2            # Sebastian
RECIPIENT = 14        # Lama (Testaccount)
GROUP = 2             # Gruppe mit sauberer Epoche
NIEMAND = 999999      # existiert weder als User noch als Gruppe

GEBREMST = "rate limit exceeded"
UNERREICHBAR = "backend unavailable"
FLUT = 40

# ------------------------------------------------------------------ Helfer ---
ERGEBNISSE: list[tuple[str, bool, str]] = []


def melde(label: str, ok: bool, text: str) -> None:
    ERGEBNISSE.append((label, ok, text))
    print(f"[{'OK    ' if ok else 'FEHLER'}] {label}: {text}")


def neue_id() -> str:
    return str(uuid.uuid4())


async def hole(ws, timeout: float = 15.0) -> dict | None:
    try:
        roh = await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    try:
        return json.loads(roh)
    except json.JSONDecodeError:
        return {"__kein_json__": roh}


async def sende(ws, payload, timeout: float = 15.0) -> dict | None:
    """Schickt eine Nachricht (dict oder roher String) und wartet auf GENAU eine Antwort."""
    await ws.send(payload if isinstance(payload, str) else json.dumps(payload))
    return await hole(ws, timeout)


FEHLT = object()  # Marker: dieses Feld darf in der Antwort NICHT vorkommen


def erwarte(label: str, antwort: dict | None, **felder) -> None:
    """Prueft, dass alle genannten Felder exakt passen. Wert FEHLT = Feld muss fehlen."""
    if antwort is None:
        melde(label, False, "keine Antwort (PROTOCOL.md 4.4 verletzt)")
        return
    if "__kein_json__" in antwort:
        melde(label, False, f"kein JSON: {antwort['__kein_json__']!r}")
        return

    abweichungen = []
    for name, wert in felder.items():
        if wert is FEHLT:
            if name in antwort:
                abweichungen.append(f"{name}={antwort[name]!r} sollte fehlen")
        elif antwort.get(name, FEHLT) != wert:
            abweichungen.append(f"{name}={antwort.get(name, '<fehlt>')!r} statt {wert!r}")

    if abweichungen:
        melde(label, False, "; ".join(abweichungen) + f"  (ganze Antwort: {antwort})")
    else:
        melde(label, True, json.dumps(antwort, ensure_ascii=False))


# ----------------------------------------------------------- Gateway-Start ---
def pruefe_exe_frisch() -> None:
    if not EXE.exists():
        sys.exit(f"{EXE} fehlt -- erst `go build` im ws-gateway-Ordner")
    neueste_go = max(p.stat().st_mtime for p in GATEWAY_DIR.glob("*.go"))
    if EXE.stat().st_mtime < neueste_go:
        sys.exit("ws-gateway.exe ist aelter als der Go-Code -- erst `go build` im "
                 "ws-gateway-Ordner, sonst testest du die alte Exe")


def port_frei(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def starte_gateway(port: int, backend_url: str | None, log: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["PORT"] = str(port)
    if backend_url is not None:
        env["BACKEND_URL"] = backend_url
    proc = subprocess.Popen(
        [str(EXE)],
        cwd=GATEWAY_DIR,  # dort liegt die .env mit den Secrets
        env=env,
        stdout=log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # fuer CTRL_BREAK beim Beenden
    )
    ende = time.time() + 10
    while time.time() < ende:
        if proc.poll() is not None:
            sys.exit(f"Gateway auf {port} ist sofort beendet -- Log: {log}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as r:
                if r.status == 200:
                    return proc
        except OSError:
            pass
        time.sleep(0.1)
    proc.kill()
    sys.exit(f"Gateway auf {port} antwortet nicht auf /healthz -- Log: {log}")


def stoppe_gateway(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.CTRL_BREAK_EVENT)  # Windows: einziges Signal, das ankommt
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ------------------------------------------------------------------- Tests ---
async def tests_normal(token: str) -> None:
    async with connect(f"ws://127.0.0.1:{PORT_OK}/ws/chat?token={token}") as ws:

        # --- 1: ack ---------------------------------------------------------
        cmid1 = neue_id()
        antwort = await sende(ws, {
            "kind": "dm", "to": RECIPIENT,
            "message": "outbox test 1 (ack)", "client_msg_id": cmid1,
        })
        erwarte("01 ack traegt client_msg_id", antwort,
                kind="ack", to=RECIPIENT, client_msg_id=cmid1)
        if antwort and antwort.get("kind") == "ack" and "client_msg_id" not in antwort:
            print("        -> Hinweis: uvicorn laeuft vermutlich noch mit dem alten internals.py")

        # --- 2: Duplikat ----------------------------------------------------
        # Genau der Outbox-Retry-Fall: Nachricht ist schon gespeichert, der
        # Client schickt sie mit derselben ID nochmal -> wieder ack.
        antwort = await sende(ws, {
            "kind": "dm", "to": RECIPIENT,
            "message": "outbox test 1 (ack)", "client_msg_id": cmid1,
        })
        erwarte("02 duplikat -> ack mit derselben ID", antwort,
                kind="ack", to=RECIPIENT, delivered_live=0, client_msg_id=cmid1)

        # --- 3: Python-error, DM an Niemand (FK-Verletzung, nichts gespeichert)
        cmid3 = neue_id()
        antwort = await sende(ws, {
            "kind": "dm", "to": NIEMAND,
            "message": "outbox test 3", "client_msg_id": cmid3,
        })
        erwarte("03 python-error dm -> retryable false", antwort,
                kind="error", retryable=False, client_msg_id=cmid3)

        # --- 4: Python-error, Gruppe existiert nicht ------------------------
        cmid4 = neue_id()
        antwort = await sende(ws, {
            "kind": "group", "to": NIEMAND, "key_version": 1,
            "message": "outbox test 4", "client_msg_id": cmid4,
        })
        erwarte("04 python-error gruppe -> retryable false", antwort,
                kind="error", retryable=False, client_msg_id=cmid4)

        # --- 5: key_outdated ------------------------------------------------
        # Auftrag, kein Fehler: ID ja, retryable NEIN (PROTOCOL.md 4.3.2).
        cmid5 = neue_id()
        antwort = await sende(ws, {
            "kind": "group", "to": GROUP, "key_version": 99,
            "message": "outbox test 5", "client_msg_id": cmid5,
        })
        erwarte("05 key_outdated -> ID, kein retryable", antwort,
                kind="key_outdated", group_chat_id=GROUP,
                client_msg_id=cmid5, retryable=FEHLT)

        # --- 6: Formfehler, kaputtes JSON -----------------------------------
        antwort = await sende(ws, '{"kind": "dm", "to": 14, "mess')
        erwarte("06 kaputtes json -> retryable false, ohne ID", antwort,
                kind="error", retryable=False, client_msg_id=FEHLT)

        # --- 7: Formfehler, JSON lesbar, ID steht sogar drin ----------------
        # Unbekanntes Feld -> parseClientMessage lehnt ab und gibt nil zurueck,
        # die ID geht dabei verloren. So in PROTOCOL.md 4.3.1 dokumentiert.
        antwort = await sende(ws, {
            "kind": "dm", "to": RECIPIENT, "message": "outbox test 7",
            "client_msg_id": neue_id(), "quatsch": 1,
        })
        erwarte("07 unbekanntes feld -> retryable false, ohne ID", antwort,
                kind="error", retryable=False, client_msg_id=FEHLT)

        # --- 8: Flut -> Ratelimit ------------------------------------------
        # Gueltige Form, aber an eine Gruppe, die es nicht gibt: was durch die
        # Bremse kommt, lehnt Python ab, BEVOR etwas gespeichert wird. So
        # entsteht bei 40 Nachrichten keine einzige DB-Zeile.
        await asyncio.sleep(2.5)  # Eimer wieder voll
        ids = [neue_id() for _ in range(FLUT)]
        for i, cmid in enumerate(ids):
            await ws.send(json.dumps({
                "kind": "group", "to": NIEMAND, "key_version": 1,
                "message": f"outbox flut {i + 1}", "client_msg_id": cmid,
            }))
        antworten = [await hole(ws, timeout=20.0) for _ in range(FLUT)]

        gebremst = durch = 0
        fehler = []
        for i, (cmid, a) in enumerate(zip(ids, antworten), start=1):
            if a is None:
                fehler.append(f"#{i} keine Antwort")
                continue
            # Go arbeitet je Verbindung strikt der Reihe nach -> Antwort i
            # gehoert zu Nachricht i. Die Outbox verlaesst sich darauf NICHT
            # (sie liest die ID), aber stimmt die ID hier nicht, stimmt sie nirgends.
            if a.get("client_msg_id") != cmid:
                fehler.append(f"#{i} ID {a.get('client_msg_id')!r} statt {cmid!r}")
            if a.get("detail") == GEBREMST:
                gebremst += 1
                if a.get("retryable") is not True:
                    fehler.append(f"#{i} gebremst, aber retryable={a.get('retryable')!r}")
            elif a.get("kind") == "error":
                durch += 1
                if a.get("retryable") is not False:
                    fehler.append(f"#{i} python-error, aber retryable={a.get('retryable')!r}")
            else:
                fehler.append(f"#{i} unerwartet: {a}")

        ok = not fehler and gebremst >= 5 and durch >= 10
        text = f"{durch} durchgelassen (python, retryable false), {gebremst} gebremst (retryable true)"
        if fehler:
            text += "; " + "; ".join(fehler[:5]) + (" ..." if len(fehler) > 5 else "")
        elif gebremst < 5:
            text += "  <- zu wenig gebremst, Flut hat die Bremse nicht erreicht"
        melde(f"08 flut von {FLUT}: jede antwort traegt IHRE ID", ok, text)


async def tests_tot(token: str) -> None:
    async with connect(f"ws://127.0.0.1:{PORT_TOT}/ws/chat?token={token}") as ws:

        # --- 9: backend unavailable ----------------------------------------
        cmid9 = neue_id()
        antwort = await sende(ws, {
            "kind": "dm", "to": RECIPIENT,
            "message": "outbox test 9", "client_msg_id": cmid9,
        }, timeout=20.0)
        erwarte("09 backend unavailable -> retryable true, ID", antwort,
                kind="error", detail=UNERREICHBAR, retryable=True, client_msg_id=cmid9)

        # --- 10: Verbindung lebt, naechste Nachricht bekommt IHRE ID --------
        cmid10 = neue_id()
        antwort = await sende(ws, {
            "kind": "dm", "to": RECIPIENT,
            "message": "outbox test 10", "client_msg_id": cmid10,
        }, timeout=20.0)
        erwarte("10 verbindung lebt, neue ID", antwort,
                kind="error", detail=UNERREICHBAR, retryable=True, client_msg_id=cmid10)


async def main() -> None:
    pruefe_exe_frisch()
    for port in (PORT_OK, PORT_TOT):
        if not port_frei(port):
            sys.exit(f"Port {port} ist belegt -- laeuft noch eine alte Instanz?")

    logdir = Path(tempfile.gettempdir())
    log_ok = logdir / "ws_outbox_gateway_8081.log"
    log_tot = logdir / "ws_outbox_gateway_8082.log"

    token = create_access_token({"user_id": str(SENDER)})

    gw_ok = starte_gateway(PORT_OK, None, log_ok)
    gw_tot = starte_gateway(PORT_TOT, TOTES_BACKEND, log_tot)
    print(f"Gateways: {PORT_OK} (normal), {PORT_TOT} (BACKEND_URL={TOTES_BACKEND})")
    print(f"Logs: {log_ok}, {log_tot}\n")

    try:
        await tests_normal(token)
        await tests_tot(token)
    finally:
        stoppe_gateway(gw_ok)
        stoppe_gateway(gw_tot)

    gruen = sum(1 for _, ok, _ in ERGEBNISSE if ok)
    print(f"\n{gruen}/{len(ERGEBNISSE)} gruen")
    if gruen != len(ERGEBNISSE):
        print("Fehlgeschlagen: " + ", ".join(l for l, ok, _ in ERGEBNISSE if not ok))
        sys.exit(1)


asyncio.run(main())
