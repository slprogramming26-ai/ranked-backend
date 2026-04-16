from passlib.context import CryptContext

# Wir setzen argon2 als primäres Schema. 
# bcrypt bleibt in der Liste, damit alte Passwörter weiterhin verifiziert werden können.
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def hash_password(password: str):
    return pwd_context.hash(password)

def verify_password(plain_password, hashed_password):
    # verify() erkennt automatisch, ob es bcrypt oder argon2 ist
    return pwd_context.verify(plain_password, hashed_password)

