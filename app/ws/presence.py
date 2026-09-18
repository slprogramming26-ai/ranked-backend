from ..redis_client import redis_client

# Go schreibt: SETEX ws:online:{user_id} 60 {instance_id}
# und erneuert alle ~25s. Wert = welche Go-Instanz, brauchen wir hier (noch) nicht.
ONLINE_TTL_SECONDS = 60


def _key(user_id: int) -> str:
    return f"ws:online:{user_id}"


def is_online(user_id: int) -> bool:
    """Für DMs: hat der Empfänger gerade eine offene Verbindung?"""
    return redis_client.get(_key(user_id)) is not None


def online_user_ids(user_ids: list[int]) -> set[int]:
    """Für den Gruppen-Fanout: von vielen Kandidaten die Online-Teilmenge."""
    if not user_ids:
        return set()

    values = redis_client.mget([_key(uid) for uid in user_ids])

    return {uid for uid, value in zip(user_ids, values) if value is not None}
