import json
import redis
from ..config import settings
from ..redis_client import redis_client

# Kanal, auf dem alle Go-Instanzen lauschen.
PUSH_CHANNEL = "ws:push"

# Eigener Kanal fuer Befehle AN Go (Sockets schliessen). Bewusst nicht ws:push:
# dessen payload oeffnet Go nie, einen Rauswurf muss es aber verstehen.
KICK_CHANNEL = "ws:kick"

# So lange kann ein gesperrter User hoechstens noch ein gueltiges Access Token
# haben. Danach ist es abgelaufen, und ohne Refresh Token (beim Bann geloescht)
# kommt kein neues. 5 Minuten Puffer gegen Uhrenversatz.
BANNED_TTL_SECONDS = settings.access_token_expire_minutes * 60 + 300

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


def _banned_key(user_id: int) -> str:
    return f"ws:banned:{user_id}"


def kick_banned(user_id: int) -> None:
    """Gesperrten User aus allen Go-Instanzen werfen und das Neuverbinden
    mit seinem noch gueltigen Access Token verhindern.

    Laeuft NACH dem commit: die Sperre steht in Postgres, egal was hier passiert."""
    try:
        # Erst den Riegel, DANN der Rauswurf. Andersherum koennte die App in der
        # Luecke dazwischen sofort neu verbinden und waere wieder drin.
        redis_client.setex(_banned_key(user_id), BANNED_TTL_SECONDS, 1)
        redis_client.publish(KICK_CHANNEL, json.dumps({
            "protocol_version": PROTOCOL_VERSION,
            "user_id": user_id,
        }))
    except redis.RedisError as e:
        # Kein 500: der Bann selbst ist gespeichert. Bekannte Restluecke: ein
        # offener Socket bleibt dann offen, bis er von selbst abreisst.
        print(f"Warnung: ws kick fehlgeschlagen fuer user {user_id}: {e}")


def lift_ban(user_id: int) -> None:
    """Gegenstueck zu kick_banned: Neuverbinden wieder erlauben."""
    try:
        redis_client.delete(_banned_key(user_id))
    except redis.RedisError as e:
        # Schlimmstenfalls kommt der User bis zum TTL-Ablauf nicht in den Chat.
        print(f"Warnung: ws ban-eintrag nicht geloescht fuer user {user_id}: {e}")
