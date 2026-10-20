"""Backup der ECHTEN DB (.env): Schema public per pg_dump (Custom-Format) + pg_cron-Jobs als SQL.
Liest nur, aendert nichts. Verbindung im Session-Modus (Pooler-Port 5432), Passwort nur per Umgebungsvariable.

Ziel: ranked/db-backups/ (neben dem Repo, AUSSERHALB von Git). Die Dateien enthalten personenbezogene
Daten, die Cron-Datei zusaetzlich das Cleanup-Secret im Klartext -> nie committen, nie hochladen.
Bilder in Supabase Storage sind NICHT enthalten.

Start:  .\\venv\\Scripts\\python.exe scripts\\backup_db.py
Restore (in leere DB):  pg_restore --no-owner --no-privileges -d <ziel-db> <datei>.dump
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # damit die .env gefunden wird

import psycopg2  # noqa: E402

from app.config import settings as s  # noqa: E402

PG_BIN = Path(r"C:\Program Files\PostgreSQL\18\bin")
ZIEL = ROOT.parent / "db-backups"
SESSION_PORT = "5432"  # Transaktions-Modus (6543) vertraegt pg_dump nicht

ZIEL.mkdir(exist_ok=True)
stempel = datetime.now().strftime("%Y-%m-%d_%H%M")
dump = ZIEL / f"ranked_public_{stempel}.dump"
cron = ZIEL / f"ranked_cron_jobs_{stempel}.sql"

env = dict(os.environ, PGPASSWORD=s.database_passwort, PGSSLMODE="require")
verbindung = ["-h", s.database_hostname, "-p", SESSION_PORT, "-U", s.database_username, "-d", s.database_name]

# 1) Dump
r = subprocess.run(
    [str(PG_BIN / "pg_dump.exe"), *verbindung, "--schema=public", "--no-owner", "--no-privileges", "-Fc", "-f", str(dump)],
    env=env, capture_output=True, text=True,
)
if r.returncode != 0:
    print("!! pg_dump fehlgeschlagen:", r.stderr.strip()[:800])
    sys.exit(1)
print(f"Dump: {dump}  ({dump.stat().st_size / 1024:.0f} KB)")

# 2) Pruefen: Inhaltsverzeichnis des Dumps lesen
r = subprocess.run([str(PG_BIN / "pg_restore.exe"), "-l", str(dump)], capture_output=True, text=True)
inhalt = r.stdout.splitlines()
tabellen = sum(" TABLE public " in z for z in inhalt)
mit_daten = sum(" TABLE DATA public " in z for z in inhalt)
print(f"Inhalt: {tabellen} Tabellen, {mit_daten} mit Daten")

conn = psycopg2.connect(host=s.database_hostname, port=SESSION_PORT, user=s.database_username,
                        password=s.database_passwort, dbname=s.database_name, sslmode="require", connect_timeout=10)
cur = conn.cursor()

# 3) Zum Vergleich: Zeilenzahlen live
cur.execute("SELECT count(*) FROM users")
print("users live:", cur.fetchone()[0])
cur.execute("SELECT count(*) FROM posts")
print("posts live:", cur.fetchone()[0])

# 4) Cron-Jobs liegen im Schema cron, nicht in public -> separat als SQL
cur.execute("SELECT jobname, schedule, command FROM cron.job ORDER BY jobid")
jobs = cur.fetchall()
with open(cron, "w", encoding="utf-8") as f:
    f.write(f"-- pg_cron-Jobs, Stand {stempel}. Wiederherstellen: im SQL-Editor ausfuehren.\n\n")
    for name, schedule, command in jobs:
        f.write(f"SELECT cron.schedule('{name}', '{schedule}', $cmd${command.strip()}$cmd$);\n\n")
print(f"Cron-Jobs: {cron}  ({len(jobs)} Jobs)")

conn.close()
