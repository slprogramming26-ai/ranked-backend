"""Setzt die Rolle eines Users direkt in der DB.

Das ist der einzige Weg, den ERSTEN Admin anzulegen — die API kann Rollen nur
vergeben, wenn es schon einen Admin gibt. Spaeter nur noch als Notausgang
(z.B. wenn sich alle Admins ausgesperrt haben).

ACHTUNG: schreibt in die DB aus der .env (aktuell Supabase = Produktion).

Start:
    venv\\Scripts\\python.exe scripts\\make_admin.py <username>              (-> admin)
    venv\\Scripts\\python.exe scripts\\make_admin.py <username> moderator
    venv\\Scripts\\python.exe scripts\\make_admin.py <username> user        (Rolle entziehen)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import models  # noqa: E402
from app.database import SessionLocal  # noqa: E402

ROLLEN = ("user", "moderator", "admin")

if len(sys.argv) not in (2, 3):
    sys.exit("Aufruf: make_admin.py <username> [user|moderator|admin]")

username = sys.argv[1]
rolle = sys.argv[2] if len(sys.argv) == 3 else "admin"

if rolle not in ROLLEN:
    sys.exit(f"Unbekannte Rolle '{rolle}', erlaubt: {', '.join(ROLLEN)}")

db = SessionLocal()
try:
    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        sys.exit(f"User '{username}' nicht gefunden")

    alt = user.role
    if alt == rolle:
        print(f"{user.username} (id {user.id}) ist schon '{rolle}' — nichts zu tun")
    else:
        user.role = rolle
        db.commit()
        print(f"{user.username} (id {user.id}): '{alt}' -> '{rolle}'")
finally:
    db.close()
