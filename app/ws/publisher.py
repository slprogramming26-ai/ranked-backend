import json
from ..redis_client import redis_client

# Kanal, auf dem alle Go-Instanzen lauschen.
PUSH_CHANNEL = "ws:push"

# Version des internen Umschlags. Steht in beiden Repos in PROTOCOL.md.
# Kennt eine Seite die Zahl nicht, verwirft sie die Nachricht, statt sie
# falsch zu deuten.
PROTOCOL_VERSION = 1


def push(target_ids: list[int], payload: dict) -> None:
    """Eine fertige Client-Nachricht an alle Go-Instanzen posaunen.

    payload MUSS schon JSON-fähig sein (also model_dump(mode="json"),
    damit datetime als ISO-String drinsteht und nicht als Python-Objekt)."""
    if not target_ids:
        return

    envelope = json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "targets": target_ids,
        "payload": payload,
    })

    try:
        redis_client.publish(PUSH_CHANNEL, envelope)
    except Exception as e:
        # Redis weg -> der Live-Push fällt aus. Das ist KEIN Grund, dem Sender
        # einen Fehler zu melden: die Nachricht steht bereits in Postgres und
        # der Empfänger holt sie per GET /messages?since= nach.
        print(f"Warnung: ws push fehlgeschlagen: {e}")
