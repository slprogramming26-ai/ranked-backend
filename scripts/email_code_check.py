"""Prueft nach einer Registrierung die email_codes-Zeile eines Users.
Liest nur, aendert nichts. Laeuft gegen die ECHTE DB (.env).

Start:  .\\venv\\Scripts\\python.exe scripts\\email_code_check.py <email> [code]

Mit [code] (aus der [mail]-Zeile im Terminal) wird zusaetzlich geprueft,
ob der Hash in der DB genau zu diesem Code passt.
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app import utils  # noqa: E402
from app.database import engine  # noqa: E402

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

email = sys.argv[1].strip().lower()
code = sys.argv[2] if len(sys.argv) > 2 else None

USER = text("SELECT id, username, email_verified FROM users WHERE email = :email")
CODES = text("""
    SELECT purpose, code_hash, attempts, expires_at, created_at
    FROM email_codes
    WHERE user_id = :uid
    ORDER BY purpose
""")

with engine.connect() as conn:
    user = conn.execute(USER, {"email": email}).first()
    if not user:
        print(f"!! Kein User mit E-Mail {email!r}.")
        sys.exit(1)
    codes = conn.execute(CODES, {"uid": user.id}).all()

fehler = 0


def check(ok: bool, text_ok: str, text_fail: str) -> None:
    global fehler
    print(f"   OK  {text_ok}" if ok else f"   !!  {text_fail}")
    if not ok:
        fehler += 1


print(f"User id={user.id} username={user.username!r} email_verified={user.email_verified}\n")
check(user.email_verified is False, "email_verified ist false", "email_verified ist schon true")

verify_rows = [r for r in codes if r.purpose == "verify"]
check(len(verify_rows) == 1, "genau eine verify-Zeile", f"{len(verify_rows)} verify-Zeilen")
if not verify_rows:
    sys.exit(1)

row = verify_rows[0]
now = datetime.now(timezone.utc)
lifetime = row.expires_at - row.created_at

check(re.fullmatch(r"[0-9a-f]{64}", row.code_hash) is not None,
      "code_hash ist ein SHA-256-Hex (64 Zeichen), kein Klartext",
      f"code_hash sieht nicht nach Hash aus: {row.code_hash!r}")
check(row.attempts == 0, "attempts = 0", f"attempts = {row.attempts}")
check(abs(lifetime - timedelta(minutes=15)) < timedelta(seconds=1),
      "expires_at = created_at + 15 min",
      f"expires_at - created_at = {lifetime}")
check(row.expires_at > now,
      f"noch gueltig (laeuft ab in {row.expires_at - now})",
      f"abgelaufen seit {now - row.expires_at}")

if code:
    check(utils.verify_code(code, row.code_hash),
          f"Code {code} passt zum Hash",
          f"Code {code} passt NICHT zum Hash")
else:
    print("   --  kein Code uebergeben, Hash-Abgleich uebersprungen")

print()
print("Alles gruen." if fehler == 0 else f"{fehler} Pruefung(en) fehlgeschlagen.")
