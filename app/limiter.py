from slowapi import Limiter
from slowapi.util import get_remote_address

# key_func bestimmt, WORAN das Limit festgemacht wird: hier die IP-Adresse.
limiter = Limiter(key_func=get_remote_address)
