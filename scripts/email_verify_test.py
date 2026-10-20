"""Prueft POST /users/verify, POST /users/verify/resend und die Login-Sperre fuer
unbestaetigte Accounts gegen einen laufenden Server.

Legt per POST /users/ einen frischen Wegwerf-User an (verify_test_<zufall>@example.com)
und loescht ihn am Ende wieder (CASCADE nimmt email_codes + refresh_tokens mit).

Der echte Code steht nur in der [mail]-Zeile im Server-Log. Damit der Test ohne
Abtippen laeuft, ueberschreibt das Skript den code_hash DIESES Test-Users direkt in
der DB mit dem Hash eines bekannten Codes. Ebenso setzt es fuer zwei Faelle kurz
attempts bzw. expires_at von Hand. Andere User werden nicht angefasst.

Achtung: .env zeigt auf Supabase -> der Test-User landet kurz in der Prod-DB.

Rate-Limits (pro IP): register 5/Minute, login 5/Minute, verify 10/Minute, resend 3/Minute.
Ein Lauf braucht 1x register + 3x login + 8x verify + 4x resend; vor dem letzten resend
wartet das Skript selbst 61 s. Zwischen zwei Laeufen ~1 Minute warten.

Start:
    venv\\Scripts\\python.exe scripts\\email_verify_test.py              (lokal, :8000)
    venv\\Scripts\\python.exe scripts\\email_verify_test.py <adresse>    (anderer Server, gleiche DB!)
"""
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app import utils  # noqa: E402
from app.database import engine  # noqa: E402

BASE = (sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000")
if not BASE.startswith(("http://", "https://")):
    BASE = "https://" + BASE

KNOWN_CODE = "424242"
WRONG_CODE = "000000"
tag = secrets.token_hex(4)
EMAIL = f"verify_test_{tag}@example.com"
USERNAME = f"verify_test_{tag}"
PASSWORT = "test-pw-123"

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


def verify(email: str, code: str) -> httpx.Response:
    return c.post("/users/verify", json={"email": email, "code": code})


def resend(email: str) -> httpx.Response:
    return c.post("/users/verify/resend", json={"email": email})


def login(email: str, passwort: str) -> httpx.Response:
    return c.post("/login", data={"username": email, "password": passwort})


c = httpx.Client(base_url=BASE, timeout=15)
print(f"Ziel: {BASE}  (Test-User {EMAIL})\n")

r = c.post("/users/", json={"email": EMAIL, "username": USERNAME, "passwort": PASSWORT, "age": 20})
if r.status_code != 201:
    print(f"Registrierung fehlgeschlagen: {r.status_code} {r.text}")
    sys.exit(1)
user_id = r.json()["id"]
print(f"  User angelegt: id={user_id}\n")

try:
    # Bekannten Code einsetzen (ersetzt den zufaelligen aus der Mail).
    db_exec("UPDATE email_codes SET code_hash = :h WHERE user_id = :uid AND purpose = 'verify'",
            h=utils.hash_code(KNOWN_CODE), uid=user_id)

    # --- Login vor der Bestaetigung ---
    r = login(EMAIL, "falsches-pw")
    pruefe("L1 unbestaetigt + falsches Passwort -> 401 (verraet nichts)",
           r.status_code == 401, f"{r.status_code} {r.json().get('detail')}")

    r = login(EMAIL, PASSWORT)
    detail = r.json().get("detail")
    pruefe("L2 unbestaetigt + richtiges Passwort -> 403 email_not_verified",
           r.status_code == 403 and isinstance(detail, dict) and detail.get("code") == "email_not_verified",
           f"{r.status_code} {detail}")
    rt = db_row("SELECT count(*) AS n FROM refresh_tokens WHERE user_id = :uid", uid=user_id)
    pruefe("L2b dabei kein Refresh-Token angelegt", rt.n == 0, f"{rt.n} Zeile(n)")

    r = verify(EMAIL, "12a456")
    pruefe("1 Code-Format falsch -> 422", r.status_code == 422, str(r.status_code))

    r = verify(f"gibtsnicht_{tag}@example.com", KNOWN_CODE)
    pruefe("2 unbekannte E-Mail -> 400", r.status_code == 400, f"{r.status_code} {r.json().get('detail')}")

    r = verify(EMAIL, WRONG_CODE)
    pruefe("3 falscher Code -> 400", r.status_code == 400, str(r.status_code))
    row = db_row("SELECT attempts FROM email_codes WHERE user_id = :uid AND purpose = 'verify'", uid=user_id)
    pruefe("3b attempts wurde gespeichert (= 1)", row is not None and row.attempts == 1,
            f"attempts = {row.attempts if row else '-'}")

    db_exec("UPDATE email_codes SET attempts = 5 WHERE user_id = :uid AND purpose = 'verify'", uid=user_id)
    r = verify(EMAIL, KNOWN_CODE)
    pruefe("4 richtiger Code, aber 5 Versuche verbraucht -> 400", r.status_code == 400, str(r.status_code))
    db_exec("UPDATE email_codes SET attempts = 0 WHERE user_id = :uid AND purpose = 'verify'", uid=user_id)

    db_exec("""UPDATE email_codes SET expires_at = now() - interval '1 minute'
               WHERE user_id = :uid AND purpose = 'verify'""", uid=user_id)
    r = verify(EMAIL, KNOWN_CODE)
    pruefe("5 richtiger Code, aber abgelaufen -> 400", r.status_code == 400, str(r.status_code))
    db_exec("""UPDATE email_codes SET expires_at = now() + interval '15 minutes'
               WHERE user_id = :uid AND purpose = 'verify'""", uid=user_id)

    # --- resend ---
    r = resend(f"gibtsnicht_{tag}@example.com")
    pruefe("R1 resend unbekannte E-Mail -> 204 (still)", r.status_code == 204, str(r.status_code))

    r = resend(EMAIL)
    detail = r.json().get("detail") if r.status_code == 429 else "-"
    pruefe("R2 resend direkt nach register -> 429 (unser Cooldown, nicht slowapi)",
           r.status_code == 429 and "warte" in str(detail), f"{r.status_code} {detail}")

    # Cooldown kuenstlich ablaufen lassen + Versuche "verbrauchen", damit man den Reset sieht.
    db_exec("""UPDATE email_codes SET created_at = now() - interval '2 minutes', attempts = 3
               WHERE user_id = :uid AND purpose = 'verify'""", uid=user_id)
    r = resend(f" {EMAIL.upper()}")
    pruefe("R3 resend nach Cooldown -> 204", r.status_code == 204, str(r.status_code))
    row = db_row("""SELECT code_hash, attempts, expires_at - created_at AS lifetime,
                           now() - created_at AS alter_
                    FROM email_codes WHERE user_id = :uid AND purpose = 'verify'""", uid=user_id)
    pruefe("R3b neuer Code (Hash ueberschrieben)", row is not None and row.code_hash != utils.hash_code(KNOWN_CODE),
           "neu" if row and row.code_hash != utils.hash_code(KNOWN_CODE) else "alter Hash noch da")
    pruefe("R3c attempts = 0, 15 min gueltig, created_at frisch",
           row is not None and row.attempts == 0 and row.lifetime == timedelta(minutes=15)
           and row.alter_ < timedelta(seconds=30),
           f"attempts={row.attempts}, lifetime={row.lifetime}, alter={row.alter_}" if row else "-")

    r = verify(EMAIL, KNOWN_CODE)
    pruefe("R4 alter Code nach resend ungueltig -> 400", r.status_code == 400, str(r.status_code))

    # Wieder bekannten Code einsetzen fuer den Erfolgsfall.
    db_exec("UPDATE email_codes SET code_hash = :h WHERE user_id = :uid AND purpose = 'verify'",
            h=utils.hash_code(KNOWN_CODE), uid=user_id)

    # E-Mail absichtlich mit Grossbuchstaben + Leerzeichen -> NormalizedEmail muss greifen.
    r = verify(f"  {EMAIL.upper()} ", KNOWN_CODE)
    body = r.json() if r.status_code == 200 else {}
    pruefe("6 richtiger Code -> 200 mit Tokens",
           r.status_code == 200 and {"access_token", "refresh_token", "token_type"} <= set(body),
           f"{r.status_code} {sorted(body) if body else r.text}")

    user = db_row("SELECT email_verified FROM users WHERE id = :uid", uid=user_id)
    pruefe("6b email_verified = true", user is not None and user.email_verified is True,
           f"email_verified = {user.email_verified if user else '-'}")
    code_row = db_row("SELECT 1 FROM email_codes WHERE user_id = :uid AND purpose = 'verify'", uid=user_id)
    pruefe("6c Code-Zeile geloescht", code_row is None, "weg" if code_row is None else "noch da")
    rt = db_row("SELECT count(*) AS n FROM refresh_tokens WHERE user_id = :uid", uid=user_id)
    pruefe("6d Refresh-Token in der DB", rt.n == 1, f"{rt.n} Zeile(n)")

    if body.get("access_token"):
        r = c.get("/users/", headers={"Authorization": f"Bearer {body['access_token']}"})
        pruefe("6e Access-Token funktioniert (GET /users/)", r.status_code == 200, str(r.status_code))

    r = login(EMAIL, PASSWORT)
    pruefe("L3 Login nach Bestaetigung -> 200 mit Tokens",
           r.status_code == 200 and "access_token" in r.json(), str(r.status_code))

    r = verify(EMAIL, KNOWN_CODE)
    pruefe("7 nochmal verify -> 409", r.status_code == 409, f"{r.status_code} {r.json().get('detail')}")

    print("\n  ... warte 61 s (resend-Limit 3/Minute ist aufgebraucht) ...\n")
    time.sleep(61)
    r = resend(EMAIL)
    pruefe("R5 resend fuer bestaetigten User -> 204 (still)", r.status_code == 204, str(r.status_code))
    code_row = db_row("SELECT 1 FROM email_codes WHERE user_id = :uid AND purpose = 'verify'", uid=user_id)
    pruefe("R5b dabei KEIN neuer Code angelegt", code_row is None, "keiner" if code_row is None else "Zeile da!")

finally:
    db_exec("DELETE FROM users WHERE id = :uid", uid=user_id)
    print(f"\n  Test-User id={user_id} wieder geloescht.")

print()
print(f"{sum(ergebnisse)}/{len(ergebnisse)} gruen." if all(ergebnisse)
      else f"{ergebnisse.count(False)} von {len(ergebnisse)} Pruefungen FEHLGESCHLAGEN.")
