"""Ersatz-Go. Lauscht auf dem Push-Kanal und zeigt, was Python rausposaunt."""
from app.redis_client import redis_client
from app.ws.publisher import PUSH_CHANNEL

sub = redis_client.pubsub()
sub.subscribe(PUSH_CHANNEL)
print(f"lausche auf {PUSH_CHANNEL} ... (Strg+C zum Beenden)")

for msg in sub.listen():
    if msg["type"] == "message":
        print(msg["data"])
