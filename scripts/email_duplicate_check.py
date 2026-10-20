"""Vor der E-Mail-Normalisierung: findet E-Mails, die nach lower(trim(...)) doppelt waeren,
und solche, die ueberhaupt Gross-Buchstaben/Leerzeichen enthalten (die schreibt die Migration um).
Liest nur, aendert nichts. Laeuft gegen die ECHTE DB (.env).

Start:  .\\venv\\Scripts\\python.exe scripts\\email_duplicate_check.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.database import engine  # noqa: E402

DUPLIKATE = text("""
    SELECT lower(trim(email)) AS norm, array_agg(id ORDER BY id) AS ids, array_agg(email ORDER BY id) AS emails
    FROM users
    GROUP BY lower(trim(email))
    HAVING count(*) > 1
""")

NICHT_NORMAL = text("""
    SELECT id, email FROM users
    WHERE email <> lower(trim(email))
    ORDER BY id
""")

with engine.connect() as conn:
    gesamt = conn.execute(text("SELECT count(*) FROM users")).scalar()
    duplikate = conn.execute(DUPLIKATE).all()
    nicht_normal = conn.execute(NICHT_NORMAL).all()

print(f"User gesamt: {gesamt}\n")

if duplikate:
    print(f"!! {len(duplikate)} Kollision(en) nach Normalisierung — Migration wuerde am UNIQUE scheitern:")
    for norm, ids, emails in duplikate:
        print(f"   {norm}: ids={ids} emails={emails}")
else:
    print("OK: keine Kollisionen nach lower(trim(email)).")

print()
if nicht_normal:
    print(f"{len(nicht_normal)} E-Mail(s) werden von der Migration umgeschrieben:")
    for uid, email in nicht_normal:
        print(f"   id={uid}: {email!r}")
else:
    print("OK: alle E-Mails sind schon klein und ohne Leerzeichen.")
