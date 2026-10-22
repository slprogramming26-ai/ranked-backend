"""Prueft DELETE /cleanup/unverified_users gegen einen laufenden Server.

Legt per SQL vier Wegwerf-User an (cleanup_test_<zufall>_<fall>@example.com) mit
zurueckdatiertem created_at, ruft den Endpoint auf und schaut, wer uebrig ist:

    A  unbestaetigt, 25 h alt, Code abgelaufen      -> muss WEG sein
    B  unbestaetigt, 25 h alt, Code noch gueltig    -> bleibt (tippt evtl. gerade)
    C  unbestaetigt, 1 h alt                        -> bleibt
    D  bestaetigt,   25 h alt                       -> bleibt

Achtung: .env zeigt auf Supabase -> der Endpoint raeumt ECHT auf der Prod-DB auf, also
auch fremde unbestaetigte Alt-Accounts (laut User gibt es dort nur eigene Test-User).
Am Ende loescht das Skript die eigenen Test-User B-D wieder.

Start:
    venv\\Scripts\\python.exe scripts\\unverified_cleanup_test.py              (lokal, :8000)
    venv\\Scripts\\python.exe scripts\\unverified_cleanup_test.py <adresse>    (anderer Server, gleiche DB!)
"""
import secrets
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import engine  # noqa: E402

BASE = (sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000")
if not BASE.startswith(("http://", "https://")):
    BASE = "https://" + BASE

tag = secrets.token_hex(4)
ergebnisse: list[bool] = []


def pruefe(name: str, ok: bool, info: str) -> None:
    print(f"  [{'OK' if ok else 'FEHLER'}] {name}: {info}")
    ergebnisse.append(ok)


def db_exec(sql: str, **params):
    with engine.begin() as conn:
        return conn.execute(text(sql), params)


def neuer_user(fall: str, alter: str, verified: bool) -> int:
    return db_exec("""INSERT INTO users (email, passwort, username, created_at, email_verified)
                      VALUES (:email, 'kein-echter-hash', :username, now() - CAST(:alter AS interval), :verified)
                      RETURNING id""",
                   email=f"cleanup_test_{tag}_{fall}@example.com", username=f"cleanup_test_{tag}_{fall}",
                   alter=alter, verified=verified).scalar()


def code(user_id: int, gueltig_noch: str) -> None:
    db_exec("""INSERT INTO email_codes (user_id, purpose, code_hash, expires_at)
               VALUES (:uid, 'verify', 'egal', now() + CAST(:rest AS interval))""",
            uid=user_id, rest=gueltig_noch)


def existiert(user_id: int) -> bool:
    return db_exec("SELECT 1 FROM users WHERE id = :uid", uid=user_id).first() is not None


ids = {
    "A": neuer_user("a", "25 hours", False),
    "B": neuer_user("b", "25 hours", False),
    "C": neuer_user("c", "1 hour", False),
    "D": neuer_user("d", "25 hours", True),
}
code(ids["A"], "-10 minutes")
code(ids["B"], "10 minutes")
print(f"Ziel: {BASE}  (Test-User {ids})\n")

c = httpx.Client(base_url=BASE, timeout=15)
try:
    r = c.delete("/cleanup/unverified_users", headers={"x-cleanup-secret": "falsch"})
    pruefe("1 falsches Secret -> 401", r.status_code == 401, str(r.status_code))
    pruefe("1b dabei nichts geloescht", existiert(ids["A"]), "A noch da" if existiert(ids["A"]) else "A weg!")

    r = c.delete("/cleanup/unverified_users", headers={"x-cleanup-secret": settings.story_cleanup_secret})
    body = r.json() if r.status_code == 200 else {}
    pruefe("2 richtiges Secret -> 200 mit deleted >= 1",
           r.status_code == 200 and body.get("deleted", 0) >= 1, f"{r.status_code} {body or r.text}")

    pruefe("A unbestaetigt, 25 h, Code abgelaufen -> weg", not existiert(ids["A"]),
           "weg" if not existiert(ids["A"]) else "noch da")
    rest = db_exec("SELECT count(*) FROM email_codes WHERE user_id = :uid", uid=ids["A"]).scalar()
    pruefe("A2 Code-Zeile per CASCADE mit weg", rest == 0, f"{rest} Zeile(n)")
    pruefe("B unbestaetigt, 25 h, Code gueltig -> bleibt", existiert(ids["B"]),
           "da" if existiert(ids["B"]) else "geloescht!")
    pruefe("C unbestaetigt, 1 h -> bleibt", existiert(ids["C"]), "da" if existiert(ids["C"]) else "geloescht!")
    pruefe("D bestaetigt, 25 h -> bleibt", existiert(ids["D"]), "da" if existiert(ids["D"]) else "geloescht!")

    if body.get("deleted", 0) > 1:
        print(f"\n  Hinweis: {body['deleted'] - 1} weitere(r) unbestaetigte(r) Alt-Account(s) mit aufgeraeumt.")

finally:
    db_exec("DELETE FROM users WHERE id = ANY(:ids)", ids=list(ids.values()))
    print(f"\n  Test-User {list(ids.values())} wieder geloescht.")

print()
print(f"{sum(ergebnisse)}/{len(ergebnisse)} gruen." if all(ergebnisse)
      else f"{ergebnisse.count(False)} von {len(ergebnisse)} Pruefungen FEHLGESCHLAGEN.")
