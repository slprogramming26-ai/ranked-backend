from passlib.context import CryptContext
import hmac
import hashlib
import secrets
from .config import settings


# Wir setzen argon2 als primäres Schema. 
# bcrypt bleibt in der Liste, damit alte Passwörter weiterhin verifiziert werden können.
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def hash_password(password: str):
    return pwd_context.hash(password)

def verify_password(plain_password, hashed_password):
    # verify() erkennt automatisch, ob es bcrypt oder argon2 ist
    return pwd_context.verify(plain_password, hashed_password)

def generate_code() -> str:
    # secrets statt random: random ist vorhersagbar, secrets ist für Sicherheit gedacht
    return f"{secrets.randbelow(1_000_000):06d}"

def hash_code(code: str) -> str:
    # HMAC = Hash mit geheimem Schlüssel. Ohne SECRET_KEY kann man aus dem Hash
    # nicht durch Ausprobieren aller 10^6 Codes zurückrechnen.
    return hmac.new(settings.secret_key.encode(), code.encode(), hashlib.sha256).hexdigest()


def verify_code(code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_code(code), code_hash)

