"""Lasttest gegen Produktion (Phase 4, nach WEB_CONCURRENCY=4). NUR LESEND.

Aufruf:  venv\\Scripts\\python.exe scripts\\prod_load_test.py [--ohne-login]

  0) Limiter: 6 falsche Logins -> 5x 401, dann 429. Sperrt die EIGENE IP
     danach bis zu 60 s fuer /login (andere Endpoints sind nicht begrenzt).
  1) Realistisch: 50 Nutzer, jeder alle 1-3 s eine Anfrage (Denkpause),
     30 s lang. Etwa das, was 50 gleichzeitig aktive Menschen erzeugen.
  2) Stress: 50 Nutzer OHNE Pause, 30 s lang. Das ist deutlich mehr als 50
     echte Menschen und zeigt, wo die Decke liegt.

Mix je Anfrage: 50 % Feed (GET /posts/, schwerste Abfrage), 30 % GET /users/,
20 % GET /ranking/leaderboard. Alle als User 2 (Token lokal erzeugt, SECRET_KEY
ist in Produktion derselbe). Jeder virtuelle Nutzer haelt EINE Keep-Alive-
Verbindung (httpx.Client) wie eine echte App, statt pro Anfrage neu TLS zu machen.
Gemessen wird vom Heimrechner aus: die Zeiten enthalten also die Internet-Strecke.
"""
import random
import statistics
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.oauth2 import create_access_token  # noqa: E402

BASE = "https://web-production-1bb6f.up.railway.app"
NUTZER = 50
DAUER = 30.0
MIX = [("feed", "/posts/", 0.5), ("me", "/users/", 0.3), ("leaderboard", "/ranking/leaderboard", 0.2)]


def waehle() -> tuple[str, str]:
    r = random.random()
    summe = 0.0
    for name, pfad, anteil in MIX:
        summe += anteil
        if r < summe:
            return name, pfad
    return MIX[-1][0], MIX[-1][1]


def pct(werte: list[float], p: float) -> float:
    if not werte:
        return float("nan")
    werte = sorted(werte)
    return werte[min(len(werte) - 1, int(round(p / 100 * (len(werte) - 1))))]


def lauf(titel: str, pause: tuple[float, float] | None, token: str) -> None:
    print(f"\n{titel}")
    ergebnisse: list[tuple[str, int, float]] = []
    sperre = threading.Lock()
    ende = time.perf_counter() + DAUER
    start_signal = threading.Barrier(NUTZER)

    def nutzer() -> None:
        with httpx.Client(base_url=BASE, timeout=30,
                          headers={"Authorization": f"Bearer {token}"}) as c:
            start_signal.wait()
            if pause:  # Startzeitpunkte verteilen, sonst kommen alle im Gleichschritt
                time.sleep(random.uniform(0, pause[1]))
            while time.perf_counter() < ende:
                name, pfad = waehle()
                t0 = time.perf_counter()
                try:
                    code = c.get(pfad).status_code
                except Exception:  # noqa: BLE001
                    code = -1
                dauer = time.perf_counter() - t0
                with sperre:
                    ergebnisse.append((name, code, dauer))
                if pause:
                    time.sleep(random.uniform(*pause))

    threads = [threading.Thread(target=nutzer) for _ in range(NUTZER)]
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    gesamt = time.perf_counter() - t_start

    codes: dict[int, int] = {}
    for _, code, _ in ergebnisse:
        codes[code] = codes.get(code, 0) + 1
    fehler = sum(n for c, n in codes.items() if c != 200)
    print(f"  {len(ergebnisse)} Anfragen in {gesamt:.1f}s = {len(ergebnisse) / gesamt:.1f} pro Sekunde")
    print(f"  Status: {codes}  ->  Fehlerquote {100 * fehler / max(1, len(ergebnisse)):.2f} %")
    print(f"  {'Endpoint':<12} {'n':>6} {'Median':>8} {'p95':>8} {'p99':>8} {'max':>8}   (ms)")
    for name in [m[0] for m in MIX] + ["ALLE"]:
        zeiten = [d * 1000 for n, c, d in ergebnisse if c == 200 and (name == "ALLE" or n == name)]
        if zeiten:
            print(f"  {name:<12} {len(zeiten):>6} {statistics.median(zeiten):>8.0f} "
                  f"{pct(zeiten, 95):>8.0f} {pct(zeiten, 99):>8.0f} {max(zeiten):>8.0f}")


def limiter_check() -> None:
    print("0) Limiter in Produktion: 6 falsche Logins")
    codes = []
    with httpx.Client(base_url=BASE, timeout=30) as c:
        for _ in range(6):
            r = c.post("/login", data={"username": "limiter-test@example.invalid", "password": "falsch"})
            codes.append(r.status_code)
    ok = codes == [401] * 5 + [429]
    print(f"  [{'OK' if ok else 'FEHLER'}] {codes}")


if __name__ == "__main__":
    if "--ohne-login" not in sys.argv:
        limiter_check()
    token = create_access_token({"user_id": "2"})
    lauf(f"1) Realistisch: {NUTZER} Nutzer, je 1-3 s Pause, {DAUER:.0f} s", (1.0, 3.0), token)
    lauf(f"2) Stress: {NUTZER} Nutzer ohne Pause, {DAUER:.0f} s", None, token)
