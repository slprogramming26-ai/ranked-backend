"""Prueft nach einer Secret-Rotation, ob lokal, Railway, Supabase und Vault
zusammenpassen. Gibt NIE einen Wert aus, nur Laenge und Fingerabdruck
(die ersten 8 Zeichen von SHA-256).

Entstanden beim Railway-Einbruch im Oktober 2026, siehe SECRET_ROTATION.md.

Was es beweist:
    - lokale Secrets sind lang genug und alle verschieden
    - ws-gateway/.env hat dieselben geteilten Werte wie das Backend
    - das deployte Backend akzeptiert einen lokal signierten Token
      (-> SECRET_KEY auf Railway == lokal, DB-Verbindung dort geht)
    - die S3-Keys koennen beide Buckets lesen
    - Vault-Werte == lokale Cron-Secrets
    - kein pg_cron-Job enthaelt noch ein Secret im Klartext

Was es NICHT prueft (eigene Skripte / von Hand):
    - Gateway + WS_INTERNAL_SECRET + Redis: scripts/ws_gateway_prod_smoke.py
    - ranking-end-of-day: nur am naechsten Morgen ueber net._http_response,
      NIE von Hand ausloesen (doppelte XP)

Start:
    venv\\Scripts\\python.exe scripts\\secret_rotation_check.py [backend-adresse]

Ohne Argument laeuft es gegen web-production-1bb6f.up.railway.app.
Schreibt nichts, loescht nichts.
"""

import hashlib
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.oauth2 import create_access_token

BACKEND = "web-production-1bb6f.up.railway.app"
GATEWAY_ENV = Path(__file__).resolve().parents[2] / "ws-gateway" / ".env"
TEST_USER = 2  # Sebastian

# Vault-Name -> lokaler Wert. Die Jobs holen sich den Wert per
# (select decrypted_secret from vault.decrypted_secrets where name = '...').
VAULT = {
    "story_cleanup_secret": settings.story_cleanup_secret,
    "ranking_job_secret": settings.ranking_job_secret,
    "group_join_code_cleanup_secret": settings.group_join_code_cleanup_secret,
}

# Alles, was wir selbst erzeugen. Muss lang sein und darf sich nie doppeln.
EIGENE = {
    "SECRET_KEY": settings.secret_key,
    "WS_INTERNAL_SECRET": settings.ws_internal_secret,
    "STORY_CLEANUP_SECRET": settings.story_cleanup_secret,
    "RANKING_JOB_SECRET": settings.ranking_job_secret,
    "GROUP_JOIN_CODE_CLEANUP_SECRET": settings.group_join_code_cleanup_secret,
    "LOCATION_ADMIN_SECRET": settings.location_admin_secret,
}

ERGEBNISSE: list[tuple[str, bool, str]] = []


def melde(label: str, ok: bool, text_: str) -> None:
    ERGEBNISSE.append((label, ok, text_))
    print(f"[{'OK    ' if ok else 'FEHLER'}] {label}: {text_}")


def fp(wert: str) -> str:
    return hashlib.sha256(wert.encode()).hexdigest()[:8]


def gateway_env() -> dict[str, str]:
    werte = {}
    for zeile in GATEWAY_ENV.read_text(encoding="utf-8").splitlines():
        k, sep, v = zeile.partition("=")
        if sep and not k.strip().startswith("#"):
            werte[k.strip()] = v.strip().strip("\"'")
    return werte


def main() -> None:
    backend = (sys.argv[1] if len(sys.argv) > 1 else BACKEND).removeprefix("https://").rstrip("/")

    # --- 1: lokale Secrets -------------------------------------------------
    # 43 Zeichen = token_urlsafe(32) = 256 Bit, darunter ist es selbst gebaut.
    kurz = [k for k, v in EIGENE.items() if len(v) < 43]
    melde("01 lokale secrets lang genug", not kurz,
          "alle >= 43 Zeichen" if not kurz else f"zu kurz: {kurz}")

    doppelt = sorted({k for k, v in EIGENE.items() if list(EIGENE.values()).count(v) > 1})
    melde("02 lokale secrets alle verschieden", not doppelt,
          "keine Dopplung" if not doppelt else f"gleicher Wert bei: {doppelt}")

    # --- 2: ws-gateway/.env -------------------------------------------------
    # Nach der Rotation 2026-10 vergessen: Railway neu, lokal alt -> jeder
    # Prod-Test scheitert mit 401/1008, obwohl Railway stimmt.
    if GATEWAY_ENV.exists():
        gw = gateway_env()
        for name in ("SECRET_KEY", "WS_INTERNAL_SECRET"):
            ok = gw.get(name) == EIGENE[name]
            melde(f"03 ws-gateway/.env {name}", ok,
                  f"fp {fp(EIGENE[name])}" if ok else f"backend fp {fp(EIGENE[name])}, gateway fp {fp(gw.get(name, ''))}")
    else:
        print(f"[--    ] 03 ws-gateway/.env: nicht gefunden ({GATEWAY_ENV}), uebersprungen")

    # --- 3: deploytes Backend ----------------------------------------------
    # 200 heisst: SECRET_KEY auf Railway == lokal UND Railway kommt in die DB
    # (get_current_user laedt den User).
    token = create_access_token({"user_id": str(TEST_USER)})
    req = urllib.request.Request(f"https://{backend}/users/",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            melde("04 backend akzeptiert lokalen SECRET_KEY", r.status == 200, f"HTTP {r.status}")
    except urllib.error.HTTPError as e:
        melde("04 backend akzeptiert lokalen SECRET_KEY", False,
              f"HTTP {e.code} <- SECRET_KEY auf Railway anders oder Deploy nicht uebernommen?")
    except Exception as e:
        melde("04 backend akzeptiert lokalen SECRET_KEY", False, f"{type(e).__name__}: {e}")

    # --- 4: S3 -------------------------------------------------------------
    # Nur gruen nach dem Widerrufen des alten Keys aussagekraeftig: vorher
    # haette auch der alte funktioniert.
    s3 = boto3.client("s3", endpoint_url=settings.s3_endpoint,
                      aws_access_key_id=settings.s3_access_key,
                      aws_secret_access_key=settings.s3_secret_key,
                      region_name=settings.s3_region)
    for bucket in ("post_images", "story_images"):
        try:
            s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
            melde(f"05 s3 {bucket} lesbar", True, "ok")
        except Exception as e:
            melde(f"05 s3 {bucket} lesbar", False, f"{type(e).__name__}: {e}")

    # --- 5 + 6: Vault und pg_cron ------------------------------------------
    with engine.connect() as c:
        vault = dict(c.execute(text(
            "select name, decrypted_secret from vault.decrypted_secrets")).all())
        for name, lokal in VAULT.items():
            v = vault.get(name)
            ok = v == lokal
            melde(f"06 vault {name}", ok,
                  f"fp {fp(lokal)}" if ok else
                  f"lokal fp {fp(lokal)}, vault " + ("FEHLT" if v is None else f"fp {fp(v)}"))

        alle_werte = list(EIGENE.values())
        for jobname, command in c.execute(text(
                "select jobname, command from cron.job order by jobid")).all():
            klartext = any(w in command for w in alle_werte)
            # Zeilenumbruch direkt nach einem ' -> so war der Story-Cleanup
            # monatelang kaputt (libcurl: bad argument).
            umbruch = bool(re.search(r"'\s*\n", command))
            aus_vault = "vault.decrypted_secrets" in command
            ok = aus_vault and not klartext and not umbruch
            grund = []
            if not aus_vault:
                grund.append("liest nicht aus dem Vault")
            if klartext:
                grund.append("Secret im Klartext")
            if umbruch:
                grund.append("Zeilenumbruch im String")
            melde(f"07 cron {jobname}", ok, "liest aus Vault" if ok else ", ".join(grund))

    gruen = sum(1 for _, ok, _ in ERGEBNISSE if ok)
    print(f"\n{gruen}/{len(ERGEBNISSE)} gruen")
    print("Danach noch: scripts\\ws_gateway_prod_smoke.py ranked-ws-gateway-production.up.railway.app")
    if gruen != len(ERGEBNISSE):
        print("Fehlgeschlagen: " + ", ".join(l for l, ok, _ in ERGEBNISSE if not ok))
        sys.exit(1)


main()
