"""Prueft die Schluessel-Backup-Routen GET/PUT/DELETE /keys/backup.

Laeuft als Testaccount Lama (14) -- NIE als echter User, weil der Test das
Backup ueberschreibt und am Ende loescht. Lamas Backup ist danach weg.

Voraussetzung: das lokale SECRET_KEY passt zum Ziel-Server (lokal immer; fuer
Railway muss es dasselbe sein wie dort, sonst kommt bei jedem Fall 401).

Das Rate-Limit (10/Minute pro IP) zaehlt ueber Laeufe hinweg: zwischen zwei
Laeufen ~1 Minute warten, sonst schlaegt schon Fall 1 mit 429 fehl.

Start:
    venv\\Scripts\\python.exe scripts\\key_backup_test.py              (lokal, :8000)
    venv\\Scripts\\python.exe scripts\\key_backup_test.py <adresse>    (z.B. Railway)
"""
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.oauth2 import create_access_token  # noqa: E402

USER_ID = 14  # Lama (Testaccount)
BASE = (sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000")
if not BASE.startswith(("http://", "https://")):
    BASE = "https://" + BASE  # nackter Hostname -> Produktion, also verschluesselt

BACKUP = {
    "secret_type": "login_password",
    "salt": "AAAAAAAAAAAAAAAAAAAAAA==",
    "nonce": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "ciphertext": "dGVzdA==",
    "opslimit": 3,
    "memlimit": 67108864,
}
FELDER = set(BACKUP) | {"updated_at"}

ergebnisse: list[bool] = []


def pruefe(name: str, ok: bool, info: str) -> None:
    print(f"  [{'OK' if ok else 'FEHLER'}] {name}: {info}")
    ergebnisse.append(ok)


token = create_access_token({"user_id": str(USER_ID)})
c = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {token}"}, timeout=15)
print(f"Ziel: {BASE}  (user_id={USER_ID})\n")

# Sauberer Start: ein Backup aus einem frueheren Lauf wuerde Fall 1 verfaelschen.
c.delete("/keys/backup")

r = httpx.get(f"{BASE}/keys/backup", timeout=15)
pruefe("0 ohne Token", r.status_code == 401, str(r.status_code))

r = c.get("/keys/backup")
pruefe("1 GET ohne Backup -> 404", r.status_code == 404, str(r.status_code))

r = c.put("/keys/backup", json=BACKUP)
pruefe("2 PUT anlegen -> 200", r.status_code == 200, str(r.status_code))
pruefe("2b Antwort hat genau die Felder", r.status_code == 200 and set(r.json()) == FELDER,
       str(sorted(r.json())) if r.status_code == 200 else "-")
erstes_update = r.json().get("updated_at") if r.status_code == 200 else None

r = c.get("/keys/backup")
gleich = r.status_code == 200 and all(r.json()[k] == v for k, v in BACKUP.items())
pruefe("3 GET liefert dieselben Werte", gleich, str(r.status_code))

time.sleep(1.1)  # sonst koennte updated_at zufaellig gleich aussehen
neu = {**BACKUP, "secret_type": "custom", "salt": "BBBBBBBBBBBBBBBBBBBBBB=="}
r = c.put("/keys/backup", json=neu)
ok = r.status_code == 200 and r.json()["secret_type"] == "custom" and r.json()["salt"] == neu["salt"]
pruefe("4 PUT ueberschreibt", ok, str(r.status_code))
zweites_update = r.json().get("updated_at") if r.status_code == 200 else None
pruefe("4b updated_at ist weitergelaufen",
       bool(erstes_update and zweites_update and zweites_update > erstes_update),
       f"{erstes_update} -> {zweites_update}")

r = c.put("/keys/backup", json={**BACKUP, "secret_type": "quatsch"})
pruefe("5 falscher secret_type -> 422", r.status_code == 422, str(r.status_code))

r = c.put("/keys/backup", json={**BACKUP, "opslimit": 0})
pruefe("6a opslimit 0 -> 422", r.status_code == 422, str(r.status_code))

r = c.put("/keys/backup", json={**BACKUP, "memlimit": 2_147_483_648})
pruefe("6b memlimit ueber INT-Max -> 422 (nicht 500)", r.status_code == 422, str(r.status_code))

r = c.put("/keys/backup", json={**BACKUP, "ciphertext": "x" * 257})
pruefe("6c ciphertext zu lang -> 422", r.status_code == 422, str(r.status_code))

r = c.get(f"/keys/{USER_ID}")
pruefe("7 alte Route /keys/{id} unberuehrt", r.status_code in (200, 404),
       f"{r.status_code} (200/404 ok, 422 hiesse: Reihenfolge kaputt)")

r = c.delete("/keys/backup")
pruefe("8 DELETE -> 204", r.status_code == 204, str(r.status_code))
r = c.get("/keys/backup")
pruefe("8b danach GET -> 404", r.status_code == 404, str(r.status_code))
r = c.delete("/keys/backup")
pruefe("8c DELETE ohne Backup -> trotzdem 204", r.status_code == 204, str(r.status_code))

# Bisher 4 GETs mit Token (der ohne Token scheitert schon an der Auth, bevor
# der Limiter zaehlt). Spaetestens die 11. Anfrage in der Minute muss gebremst werden.
codes = [c.get("/keys/backup").status_code for _ in range(10)]
pruefe("9 Rate-Limit greift -> 429", 429 in codes, str(codes))

c.close()
print(f"\n{sum(ergebnisse)}/{len(ergebnisse)} gruen.")
sys.exit(0 if all(ergebnisse) else 1)
