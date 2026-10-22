"""Prueft POST /users/password/forgot und POST /users/password/reset gegen einen laufenden Server.

Legt per POST /users/ einen frischen Wegwerf-User an (reset_test_<zufall>@example.com,
absichtlich UNbestaetigt) und loescht ihn am Ende wieder (CASCADE nimmt email_codes +
refresh_tokens mit). Der Redis-Zaehler pw_reset_fail:<id> wird am Ende ebenfalls geloescht.

Wie bei email_verify_test.py: Der echte Code steht nur im Server-Log, deshalb ueberschreibt
das Skript den code_hash DIESES Test-Users direkt in der DB mit dem Hash eines bekannten
Codes und setzt fuer einzelne Faelle attempts/expires_at von Hand. Den Tageszaehler setzt
es direkt in Redis, statt 10x falsch zu raten (das wuerde das 10/Minute-Limit sprengen).
Zwei Refresh-Token-Zeilen ("zwei Handys") legt es per SQL an, um das Ausloggen zu pruefen.

Achtung: .env zeigt auf Supabase -> der Test-User landet kurz in der Prod-DB.
Server und Skript muessen dieselbe REDIS_URL nutzen (beide aus .env).

Rate-Limits (pro IP): register 5/Minute, forgot 3/Minute, reset 10/Minute, login 5/Minute.
Ein Lauf braucht 1x register + 3x forgot + 9x reset + 2x login. Zwischen zwei Laeufen
~1 Minute warten.

Start:
    venv\\Scripts\\python.exe scripts\\password_reset_test.py              (lokal, :8000)
    venv\\Scripts\\python.exe scripts\\password_reset_test.py <adresse>    (anderer Server, gleiche DB + Redis!)
"""
import secrets
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app import utils  # noqa: E402
from app.database import engine  # noqa: E402
from app.redis_client import redis_client  # noqa: E402

BASE = (sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000")
if not BASE.startswith(("http://", "https://")):
    BASE = "https://" + BASE

KNOWN_CODE = "424242"
WRONG_CODE = "000000"
tag = secrets.token_hex(4)
EMAIL = f"reset_test_{tag}@example.com"
USERNAME = f"reset_test_{tag}"
ALTES_PW = "altes-pw-123"
NEUES_PW = "neues-pw-456"

ergebnisse: list[bool] = []


def pruefe(name: str, ok: bool, info: str) -> None:
    print(f"  [{'OK' if ok else 'FEHLER'}] {name}: {info}")
    ergebnisse.append(ok)


def db_exec(sql: str, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def db_row(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).first()


def forgot(email: str) -> httpx.Response:
    return c.post("/users/password/forgot", json={"email": email})


def reset(email: str, code: str, new_password: str = NEUES_PW) -> httpx.Response:
    return c.post("/users/password/reset", json={"email": email, "code": code, "new_password": new_password})


def login(email: str, passwort: str) -> httpx.Response:
    return c.post("/login", data={"username": email, "password": passwort})


def detail(r: httpx.Response):
    try:
        return r.json().get("detail")
    except ValueError:
        return "-"


c = httpx.Client(base_url=BASE, timeout=15)
print(f"Ziel: {BASE}  (Test-User {EMAIL})\n")

r = c.post("/users/", json={"email": EMAIL, "username": USERNAME, "passwort": ALTES_PW, "age": 20})
if r.status_code != 201:
    print(f"Registrierung fehlgeschlagen: {r.status_code} {r.text}")
    sys.exit(1)
user_id = r.json()["id"]
fail_key = f"pw_reset_fail:{user_id}"
print(f"  User angelegt: id={user_id} (unbestaetigt)\n")


def reset_code_row():
    return db_row("""SELECT code_hash, attempts FROM email_codes
                     WHERE user_id = :uid AND purpose = 'reset'""", uid=user_id)


def fails() -> int:
    value = redis_client.get(fail_key)
    return int(value) if value else 0


try:
    # --- forgot ---
    r = forgot(f"gibtsnicht_{tag}@example.com")
    pruefe("F1 forgot unbekannte E-Mail -> 204 (still)", r.status_code == 204, str(r.status_code))

    r = forgot(f"  {EMAIL.upper()} ")
    pruefe("F2 forgot (Grossbuchstaben + Leerzeichen) -> 204", r.status_code == 204, str(r.status_code))
    pruefe("F2b reset-Code-Zeile angelegt", reset_code_row() is not None,
           "da" if reset_code_row() is not None else "fehlt")
    verify_row = db_row("SELECT 1 FROM email_codes WHERE user_id = :uid AND purpose = 'verify'", uid=user_id)
    pruefe("F2c verify-Code vom register bleibt daneben bestehen", verify_row is not None,
           "da" if verify_row is not None else "fehlt")

    r = forgot(EMAIL)
    pruefe("F3 forgot direkt nochmal -> 429 (unser Cooldown, nicht slowapi)",
           r.status_code == 429 and "warte" in str(detail(r)), f"{r.status_code} {detail(r)}")

    # Bekannten Code einsetzen + zwei "Handys" eingeloggt simulieren.
    db_exec("UPDATE email_codes SET code_hash = :h WHERE user_id = :uid AND purpose = 'reset'",
            h=utils.hash_code(KNOWN_CODE), uid=user_id)
    for _ in range(2):
        db_exec("""INSERT INTO refresh_tokens (user_id, token_hash, expires_at)
                   VALUES (:uid, :h, now() + interval '30 days')""",
                uid=user_id, h=secrets.token_hex(32))

    # --- reset: Validierung ---
    r = reset(EMAIL, "12a456")
    pruefe("P1 Code-Format falsch -> 422", r.status_code == 422, str(r.status_code))

    r = reset(EMAIL, KNOWN_CODE, new_password="kurz")
    pruefe("P2 neues Passwort < 8 Zeichen -> 422", r.status_code == 422, str(r.status_code))

    r = reset(f"gibtsnicht_{tag}@example.com", KNOWN_CODE)
    pruefe("P3 unbekannte E-Mail -> 400", r.status_code == 400, f"{r.status_code} {detail(r)}")

    # --- reset: Fehlversuche ---
    r = reset(EMAIL, WRONG_CODE)
    row = reset_code_row()
    pruefe("P4 falscher Code -> 400", r.status_code == 400, str(r.status_code))
    pruefe("P4b attempts = 1 gespeichert", row is not None and row.attempts == 1,
           f"attempts = {row.attempts if row else '-'}")
    pruefe("P4c Redis-Tageszaehler = 1", fails() == 1, f"{fails()}")
    ttl = redis_client.ttl(fail_key)
    pruefe("P4d Zaehler hat Ablaufzeit (~24 h)", 86000 < ttl <= 86400, f"TTL = {ttl} s")

    db_exec("UPDATE email_codes SET attempts = 5 WHERE user_id = :uid AND purpose = 'reset'", uid=user_id)
    r = reset(EMAIL, KNOWN_CODE)
    pruefe("P5 richtiger Code, aber 5 Versuche verbraucht -> 400", r.status_code == 400, str(r.status_code))
    pruefe("P5b Tageszaehler NICHT erhoeht (kein echtes Raten)", fails() == 1, f"{fails()}")
    db_exec("UPDATE email_codes SET attempts = 0 WHERE user_id = :uid AND purpose = 'reset'", uid=user_id)

    db_exec("""UPDATE email_codes SET expires_at = now() - interval '1 minute'
               WHERE user_id = :uid AND purpose = 'reset'""", uid=user_id)
    r = reset(EMAIL, KNOWN_CODE)
    pruefe("P6 richtiger Code, aber abgelaufen -> 400", r.status_code == 400, str(r.status_code))
    pruefe("P6b Tageszaehler NICHT erhoeht", fails() == 1, f"{fails()}")
    db_exec("""UPDATE email_codes SET expires_at = now() + interval '15 minutes'
               WHERE user_id = :uid AND purpose = 'reset'""", uid=user_id)

    # --- reset: Tagesbremse ---
    redis_client.set(fail_key, 10, ex=86400)
    r = reset(EMAIL, KNOWN_CODE)
    pruefe("P7 Tageszaehler voll -> 429 trotz richtigem Code",
           r.status_code == 429 and "morgen" in str(detail(r)), f"{r.status_code} {detail(r)}")
    row = reset_code_row()
    pruefe("P7b Code-Zeile unangetastet (attempts = 0)", row is not None and row.attempts == 0,
           f"attempts = {row.attempts if row else '-'}")
    redis_client.set(fail_key, 3, ex=86400)

    # --- reset: Erfolg ---
    r = reset(f"  {EMAIL.upper()} ", KNOWN_CODE)
    pruefe("P8 richtiger Code -> 204", r.status_code == 204, f"{r.status_code} {r.text}")

    user = db_row("SELECT passwort, email_verified FROM users WHERE id = :uid", uid=user_id)
    pruefe("P8b neues Passwort gespeichert", user is not None and utils.verify_password(NEUES_PW, user.passwort),
           "neu" if user and utils.verify_password(NEUES_PW, user.passwort) else "alt/kaputt")
    pruefe("P8c email_verified = true (war vorher false)", user is not None and user.email_verified is True,
           f"email_verified = {user.email_verified if user else '-'}")
    codes = db_row("SELECT count(*) AS n FROM email_codes WHERE user_id = :uid", uid=user_id)
    pruefe("P8d ALLE Codes weg (reset + verify)", codes.n == 0, f"{codes.n} Zeile(n)")
    rt = db_row("SELECT count(*) AS n FROM refresh_tokens WHERE user_id = :uid", uid=user_id)
    pruefe("P8e beide Refresh-Tokens geloescht (alle Geraete ausgeloggt)", rt.n == 0, f"{rt.n} Zeile(n)")
    pruefe("P8f Redis-Tageszaehler geleert", redis_client.exists(fail_key) == 0,
           "weg" if redis_client.exists(fail_key) == 0 else f"noch {fails()}")

    r = reset(EMAIL, KNOWN_CODE)
    pruefe("P9 gleicher Code nochmal -> 400 (Code verbraucht)", r.status_code == 400, str(r.status_code))

    # --- Login danach ---
    r = login(EMAIL, ALTES_PW)
    pruefe("L1 Login mit altem Passwort -> 401", r.status_code == 401, str(r.status_code))

    r = login(EMAIL, NEUES_PW)
    pruefe("L2 Login mit neuem Passwort -> 200 (auch ohne vorheriges verify)",
           r.status_code == 200 and "access_token" in r.json(), f"{r.status_code} {detail(r)}")

finally:
    db_exec("DELETE FROM users WHERE id = :uid", uid=user_id)
    redis_client.delete(fail_key)
    print(f"\n  Test-User id={user_id} + Redis-Zaehler wieder geloescht.")

print()
print(f"{sum(ergebnisse)}/{len(ergebnisse)} gruen." if all(ergebnisse)
      else f"{ergebnisse.count(False)} von {len(ergebnisse)} Pruefungen FEHLGESCHLAGEN.")
