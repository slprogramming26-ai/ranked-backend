"""Testreihe 1-6 fuer die internen WS-Endpoints — ersetzt die Postman-Klickerei.

Voraussetzung: uvicorn laeuft auf 127.0.0.1:8000 (ohne --reload).
Aufruf:        venv\\Scripts\\python.exe -m scripts.ws_internal_test

Das Secret wird aus settings gelesen und NIE ausgegeben.
Der Lauscher auf ws:push laeuft als Thread mit — kein zweites Terminal noetig.
"""
import json
import threading
import time
import uuid

import httpx

from app.config import settings
from app.redis_client import redis_client
from app.ws.publisher import PROTOCOL_VERSION, PUSH_CHANNEL

BASE = "http://127.0.0.1:8000"
SENDER = 2           # Sebastian
RECIPIENT = 14       # Lama (Testaccount)
GROUP = 2            # sauber, hat Epoche
GROUP_CURRENT_KV = 1  # aktuelle key_version dieser Gruppe

# ---------------------------------------------------------------- Lauscher ---
# Faengt alle Umschlaege auf ws:push ein, damit Test 3 pruefen kann, ob
# wirklich rausgepusht wurde. Ersetzt scripts/ws_listen.py im zweiten Fenster.
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
    """Wartet bis zu `wartezeit` auf den Umschlag mit dieser client_msg_id."""
    ende = time.time() + wartezeit
    while time.time() < ende:
        with _lock:
            for env in _envelopes:
                if env.get("payload", {}).get("client_msg_id") == client_msg_id:
                    return env
        time.sleep(0.05)
    return None


# ------------------------------------------------------------------ Helfer ---
_ergebnisse: list[tuple[str, bool, str]] = []


def pruefe(name: str, bedingung: bool, details: str) -> None:
    _ergebnisse.append((name, bedingung, details))
    marke = "OK  " if bedingung else "FEHL"
    print(f"[{marke}] {name}")
    for zeile in details.splitlines():
        print(f"        {zeile}")
    print()


def post(pfad: str, body: dict, mit_secret: bool = True) -> httpx.Response:
    headers = {"X-WS-Secret": settings.ws_internal_secret} if mit_secret else {}
    return httpx.post(f"{BASE}{pfad}", json=body, headers=headers, timeout=30.0)


def dm_body(**overrides) -> dict:
    body = {
        "protocol_version": PROTOCOL_VERSION,
        "sender_id": SENDER,
        "to": RECIPIENT,
        "message": "ws-internal-test",
        "client_msg_id": str(uuid.uuid4()),  # pro Aufruf neu -> kein Duplikat-Schutz
    }
    body.update(overrides)
    return body


def presence_key(user_id: int) -> str:
    return f"ws:online:{user_id}"


# ------------------------------------------------------------------- Tests ---
def main() -> None:
    threading.Thread(target=_listen, daemon=True).start()
    time.sleep(0.5)  # Abo muss stehen, bevor der erste Push rausgeht

    # Sauberer Start: eine Presence aus einem frueheren Lauf wuerde Test 3
    # und Test 5 verfaelschen.
    redis_client.delete(presence_key(RECIPIENT))

    # --- 1: ohne Secret -> 401 -------------------------------------------
    r = post("/internal/ws/dm", dm_body(), mit_secret=False)
    pruefe(
        "Test 1 — ohne Secret wird abgewiesen",
        r.status_code == 401,
        f"erwartet 401, bekommen {r.status_code}\nbody: {r.text[:200]}",
    )

    # --- 2: falsche protocol_version -> 422 ------------------------------
    r = post("/internal/ws/dm", dm_body(protocol_version=99))
    pruefe(
        "Test 2 — protocol_version 99 wird abgewiesen",
        r.status_code == 422,
        f"erwartet 422, bekommen {r.status_code}",
    )

    # --- 3: gueltige DM -> Ack + Umschlag auf ws:push --------------------
    body = dm_body()
    r = post("/internal/ws/dm", body)
    ack = r.json() if r.status_code == 200 else {}
    env = envelope_for(body["client_msg_id"])
    ok = (
        r.status_code == 200
        and ack.get("to") == RECIPIENT
        and env is not None
        and env.get("targets") == [RECIPIENT]
        and env.get("protocol_version") == PROTOCOL_VERSION
        and env.get("payload", {}).get("sender_id") == SENDER
    )
    pruefe(
        "Test 3 — gueltige DM: Ack und Umschlag im Lauscher",
        ok,
        f"status: {r.status_code}\nack: {json.dumps(ack)}\n"
        f"umschlag: {json.dumps(env) if env else 'KEINER angekommen'}",
    )

    # --- 4: Presence gesetzt -> delivered_live == 1 ----------------------
    redis_client.setex(presence_key(RECIPIENT), 60, "test-instance")
    r = post("/internal/ws/dm", dm_body())
    ack = r.json() if r.status_code == 200 else {}
    pruefe(
        "Test 4 — Presence gesetzt: delivered_live == 1",
        ack.get("delivered_live") == 1,
        f"status: {r.status_code}\nack: {json.dumps(ack)}\n"
        f"ttl von ws:online:{RECIPIENT}: {redis_client.ttl(presence_key(RECIPIENT))}s",
    )

    # --- 5: TTL laeuft ab -> delivered_live == 0 -------------------------
    # 2s statt 60s, damit der Test nicht eine Minute steht. Geprueft wird die
    # Mechanik (Key verschwindet von selbst), nicht die konkrete Zahl 60.
    redis_client.setex(presence_key(RECIPIENT), 2, "test-instance")
    time.sleep(3)
    noch_da = redis_client.get(presence_key(RECIPIENT))
    r = post("/internal/ws/dm", dm_body())
    ack = r.json() if r.status_code == 200 else {}
    pruefe(
        "Test 5 — nach TTL-Ablauf: delivered_live == 0",
        noch_da is None and ack.get("delivered_live") == 0,
        f"key nach 3s: {noch_da!r} (erwartet None)\nack: {json.dumps(ack)}",
    )

    # --- 6: Gruppe mit falscher key_version -> key_outdated --------------
    # Schreibt NICHTS in die DB: der Tuersteher wirft vor dem Speichern.
    r = post(
        "/internal/ws/group",
        {
            "protocol_version": PROTOCOL_VERSION,
            "sender_id": SENDER,
            "to": GROUP,
            "message": "ws-internal-test",
            "client_msg_id": str(uuid.uuid4()),
            "key_version": 99,
        },
    )
    antwort = r.json() if r.status_code == 200 else {}
    pruefe(
        "Test 6 — falsche key_version: kind == 'key_outdated' (nicht 'error')",
        r.status_code == 200
        and antwort.get("kind") == "key_outdated"
        and antwort.get("current_version") == GROUP_CURRENT_KV,
        f"status: {r.status_code}\nantwort: {json.dumps(antwort)}",
    )

    # --- Aufraeumen -------------------------------------------------------
    redis_client.delete(presence_key(RECIPIENT))

    print("=" * 60)
    bestanden = sum(1 for _, ok, _ in _ergebnisse if ok)
    print(f"{bestanden} von {len(_ergebnisse)} bestanden")
    for name, ok, _ in _ergebnisse:
        print(f"  {'OK  ' if ok else 'FEHL'}  {name}")


if __name__ == "__main__":
    main()
