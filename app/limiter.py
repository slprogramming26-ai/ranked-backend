from slowapi import Limiter
from slowapi.util import get_remote_address
from .config import settings

# key_func bestimmt, WORAN das Limit festgemacht wird: hier die IP-Adresse.
# storage_uri: Zaehler liegen in Redis statt im Prozess-RAM. Nur so teilen sich
# mehrere Worker EINEN Zaehler (sonst hat jeder Worker seine eigenen 5/Minute).
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.redis_url,
    # Redis weg -> kein 500 beim Login, sondern vorübergehend im RAM zählen
    # (pro Worker). slowapi prüft selbst, wann Redis wieder da ist.
    in_memory_fallback_enabled=True,
    # Ohne Timeout würde ein hängendes Redis den Login mit aufhängen.
    storage_options={"socket_connect_timeout": 1, "socket_timeout": 1},
)

