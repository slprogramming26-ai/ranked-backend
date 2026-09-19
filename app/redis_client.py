import redis
from .config import settings

# Ohne Angabe nimmt redis-py 8 je 5 s. Zu lang: internals.py macht zwei
# Redis-Aufrufe hintereinander (publish + Presence), 2 x 5 s = genau Gos
# 10-s-Backend-Timeout. 1 s ist bei ~1 ms Normalzeit reichlich (wie limiter.py).
redis_client = redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=1,
    socket_timeout=1,
)

