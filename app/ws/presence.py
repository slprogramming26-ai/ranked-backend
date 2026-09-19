import redis

from ..redis_client import redis_client

# Go schreibt: SETEX ws:online:{user_id} 60 {instance_id}
# und erneuert alle ~25s. Wert = welche Go-Instanz, brauchen wir hier (noch) nicht.
ONLINE_TTL_SECONDS = 60

# Beide Funktionen laufen in internals.py NACH dem Speichern und liefern nur die
# Zahl delivered_live im Ack. Ist Redis weg, wird diese Zahl falsch (0) - mehr
# nicht. Ohne das try/except gäbe es ein 500, Go meldete "backend unavailable",
# und der Sender hielte eine längst gespeicherte Nachricht für verloren.
# Bewusst nur RedisError (umfasst Timeout + Verbindungsfehler): ein echter
# Programmierfehler soll weiter laut auffallen.


def _key(user_id: int) -> str:
    return f"ws:online:{user_id}"


def is_online(user_id: int) -> bool:
    """Für DMs: hat der Empfänger gerade eine offene Verbindung?"""
    try:
        return redis_client.get(_key(user_id)) is not None
    except redis.RedisError as e:
        print(f"Warnung: presence-Abfrage fehlgeschlagen: {e}")
        return False


def online_user_ids(user_ids: list[int]) -> set[int]:
    """Für den Gruppen-Fanout: von vielen Kandidaten die Online-Teilmenge."""
    if not user_ids:
        return set()

    try:
        values = redis_client.mget([_key(uid) for uid in user_ids])
    except redis.RedisError as e:
        print(f"Warnung: presence-Abfrage fehlgeschlagen: {e}")
        return set()

    return {uid for uid, value in zip(user_ids, values) if value is not None}
