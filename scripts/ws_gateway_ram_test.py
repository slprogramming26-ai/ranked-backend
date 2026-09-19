"""Misst den RAM pro WebSocket am Go-Gateway - die Nachher-Zahl zu Phase 0.

Phase 0 (2026-08-29) hat unter FastAPI/uvicorn ~210 KB pro offener
WebSocket-Verbindung gemessen. Dieses Skript wiederholt die Messung mit
demselben Protokoll gegen ws-gateway.exe:

  - Dienst vor JEDER Stufe frisch starten (sonst misst man die Reste der
    vorigen Stufe mit)
  - Leerwert VOR der Last
  - RSS = WorkingSetSize (dieselbe Zahl, die Get-CimInstance Win32_Process
    liefert), Gegenprobe mit Get-NetTCPConnection -LocalPort 8080
  - ein einziger Token fuer alle Verbindungen, Rampe knapp 10/s

Ein Unterschied zu Phase 0, bewusst: gemessen wird zweimal - 30 s nach der
Rampe (vergleichbar mit Phase 0) und nach 130 s = Dauerbetrieb. Bis dahin
ist jede Verbindung mehrmals durch den Heartbeat gelaufen (alle 25 s SET in
Redis + Ping), ihre Goroutine-Stacks und Puffer sind ausgewachsen, und die
Laufzeit hat mindestens eine GC erzwungen (spaetestens alle 2 min).
Gemessen am 2026-09-19: der 130-s-Wert liegt HOEHER als der 30-s-Wert, er
ist die ehrliche Zahl.

Die Rampe bleibt knapp unter connRate (10/s, Burst 30) aus ratelimit.go,
sonst misst man die IP-Bremse. Abgelehnte Versuche (429) werden
wiederholt und gezaehlt.

Start:  venv\\Scripts\\python.exe scripts\\ws_gateway_ram_test.py [500 1000 2000] [--schnell] [--gctrace]
        --schnell = nur der 30-s-Wert, ohne den Dauerbetrieb abzuwarten
        --gctrace = Go meldet jede GC (GODEBUG=gctrace=1); am Ende steht dann,
                    wie viel davon lebender Heap und wie viel Goroutine-Stacks
                    sind. Ohne Codeaenderung am Gateway.

Voraussetzung: Port 8080 ist FREI (das Skript startet den Dienst selbst),
ws-gateway.exe ist frisch gebaut, lokales Redis laeuft.
Schreibt nichts in die DB: es gehen nur Handshakes ueber die Leitung.
"""

import asyncio
import ctypes
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from app.oauth2 import create_access_token

GATEWAY_DIR = Path(r"C:\Users\Sebastian\Documents\Programmieren\Projekte\ranked\ws-gateway")
EXE = GATEWAY_DIR / "ws-gateway.exe"

PORT = 8080
BASE = f"ws://127.0.0.1:{PORT}/ws/chat"
USER = 2

# Verbindungen pro Sekunde. connRate in ratelimit.go ist 10.
RATE = 9
# Zeitpunkte nach der Rampe, zu denen gemessen wird (je Median ueber 10 s davor).
KURZ = 30
LANG = 130


# --- Speicher des Gateway-Prozesses ---------------------------------------

class _Speicher(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
_k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MB = 1024 * 1024  # wie PowerShells 1MB - Phase 0 hat so gerechnet


def speicher_mb(pid: int) -> tuple[float, float]:
    """(WorkingSet, Private) in MB."""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        m = _Speicher()
        m.cb = ctypes.sizeof(m)
        if not _k32.K32GetProcessMemoryInfo(h, ctypes.byref(m), m.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return m.WorkingSetSize / MB, m.PrivateUsage / MB
    finally:
        _k32.CloseHandle(h)


def established() -> int:
    """Gegenprobe wie in Phase 0: wie viele Sockets haelt der Server wirklich?"""
    befehl = (
        f"(Get-NetTCPConnection -LocalPort {PORT} -State Established "
        "-ErrorAction SilentlyContinue | Measure-Object).Count"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", befehl],
        capture_output=True, text=True, timeout=60,
    )
    return int(out.stdout.strip() or 0)


# --- Dienst ---------------------------------------------------------------

def port_frei() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) != 0


def warte_auf_start(p: subprocess.Popen, sekunden: float = 10.0) -> bool:
    ende = time.monotonic() + sekunden
    while time.monotonic() < ende:
        if p.poll() is not None:
            return False
        if not port_frei():
            return True
        time.sleep(0.1)
    return False


def exe_aktuell() -> bool:
    """Warnt vor dem Stolperstein 'alte ws-gateway.exe laeuft noch'."""
    neueste_quelle = max(f.stat().st_mtime for f in GATEWAY_DIR.glob("*.go"))
    return EXE.stat().st_mtime >= neueste_quelle


# --- Last -----------------------------------------------------------------

async def halte(url: str, z: dict, ende: asyncio.Event) -> None:
    """Oeffnet eine Verbindung und haelt sie, bis der Dienst sie schliesst."""
    for versuch in range(10):
        try:
            async with connect(url, open_timeout=30) as ws:
                z["offen"] += 1
                await ws.wait_closed()
                if not ende.is_set():
                    z["vorzeitig"] += 1
                return
        except InvalidStatus as e:
            if e.response.status_code == 429:
                z["429"] += 1
                await asyncio.sleep(1 + versuch)
                continue
            z["fehler"][f"HTTP {e.response.status_code}"] = z["fehler"].get(f"HTTP {e.response.status_code}", 0) + 1
            return
        except (ConnectionRefusedError, TimeoutError):
            await asyncio.sleep(1 + versuch)
            continue
        except Exception as e:  # alles andere zaehlen statt abzustuerzen
            name = type(e).__name__
            z["fehler"][name] = z["fehler"].get(name, 0) + 1
            return
    z["fehler"]["aufgegeben"] = z["fehler"].get("aufgegeben", 0) + 1


def median_zwischen(verlauf: list, von: float, bis: float) -> tuple[float, float]:
    fenster = [(ws, pr) for t, ws, pr in verlauf if von <= t <= bis]
    return (
        statistics.median(ws for ws, _ in fenster),
        statistics.median(pr for _, pr in fenster),
    )


# gc 12 @130.2s 0%: ... ms cpu, 9->9->5 MB, 10 MB goal, 3 MB stacks, 0 MB globals, 8 P
GC_ZEILE = re.compile(r"gc \d+ @([\d.]+)s .*?(\d+)->(\d+)->(\d+) MB, (\d+) MB goal, (\d+) MB stacks")


def letzte_gc(log: str, bis: float) -> dict | None:
    """Letzte GC vor dem Herunterfahren: lebender Heap danach und Stacks.

    bis = Sekunden seit Prozessstart; spaetere GCs fallen schon ins
    Aufraeumen und wuerden zu wenig zeigen."""
    treffer = [g for g in GC_ZEILE.findall(log) if float(g[0]) < bis]
    if not treffer:
        return None
    t, _, _, live, ziel, stacks = treffer[-1]
    return {"t": float(t), "live": int(live), "ziel": int(ziel), "stacks": int(stacks)}


async def stufe(n: int, token: str, lang: float, gctrace: bool) -> dict:
    logpfad = Path(tempfile.gettempdir()) / f"ws_gateway_ram_{n}.log"
    # Go loggt jede Verbindung. In eine PIPE geschrieben, die niemand leert,
    # wuerde Go nach ~4 KB Log mitten im Handshake stehen bleiben -> Datei.
    log = open(logpfad, "w", encoding="utf-8")
    env = dict(os.environ)
    if gctrace:
        env["GODEBUG"] = "gctrace=1"
    p = subprocess.Popen(
        [str(EXE)],
        cwd=str(GATEWAY_DIR),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )
    t_prozess = time.monotonic()
    try:
        if not warte_auf_start(p):
            raise RuntimeError(f"Gateway nicht hochgekommen, siehe {logpfad}")

        await asyncio.sleep(3)
        proben = []
        for _ in range(5):
            proben.append(speicher_mb(p.pid))
            await asyncio.sleep(0.5)
        leer_ws = statistics.median(ws for ws, _ in proben)
        leer_pr = statistics.median(pr for _, pr in proben)
        print(f"  Leer: {leer_ws:.1f} MB (privat {leer_pr:.1f} MB)")

        z = {"offen": 0, "vorzeitig": 0, "429": 0, "fehler": {}}
        ende = asyncio.Event()
        url = f"{BASE}?token={token}"

        t0 = time.monotonic()
        tasks = []
        for i in range(n):
            tasks.append(asyncio.create_task(halte(url, z, ende)))
            if (i + 1) % RATE == 0:
                await asyncio.sleep(1.0)
                if (i + 1) % (RATE * 20) == 0:
                    print(f"  ... {i + 1} gestartet, offen {z['offen']}, 429: {z['429']}")

        frist = time.monotonic() + 60
        while z["offen"] + sum(z["fehler"].values()) < n and time.monotonic() < frist:
            await asyncio.sleep(0.5)
        print(f"  Rampe fertig nach {time.monotonic() - t0:.0f} s: offen {z['offen']} von {n}")

        verlauf = []
        tcp = None
        t_ruhe = time.monotonic()
        while (t := time.monotonic() - t_ruhe) <= lang:
            ws, pr = speicher_mb(p.pid)
            verlauf.append((t, ws, pr))
            if tcp is None and t >= KURZ - 8:
                tcp = await asyncio.to_thread(established)
            await asyncio.sleep(2)

        kurz_ws, kurz_pr = median_zwischen(verlauf, KURZ - 10, KURZ)
        lang_ws, lang_pr = median_zwischen(verlauf, lang - 10, lang)
        spitze = max(ws for _, ws, _ in verlauf)

        # Aufraeumen: sauberes Herunterfahren wie auf Railway.
        ende.set()
        t_stop = time.monotonic() - t_prozess
        os.kill(p.pid, signal.CTRL_BREAK_EVENT)
        await asyncio.to_thread(p.wait, 60)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
        log.close()

    logtext = logpfad.read_text(encoding="utf-8", errors="replace")
    abgelehnt = logtext.count("abgelehnt: ratelimit")
    gc = letzte_gc(logtext, t_stop - 0.5) if gctrace else None

    return {
        "n": n,
        "offen": z["offen"],
        "tcp": tcp,
        "vorzeitig": z["vorzeitig"],
        "429": z["429"],
        "429_log": abgelehnt,
        "fehler": z["fehler"],
        "leer_ws": leer_ws, "leer_pr": leer_pr,
        "kurz_ws": kurz_ws, "kurz_pr": kurz_pr,
        "lang_ws": lang_ws, "lang_pr": lang_pr,
        "spitze": spitze,
        "gc": gc,
        "exit": p.returncode,
        "log": logpfad,
    }


def kb_pro(diff_mb: float, n: int) -> float:
    return diff_mb * 1024 / n


async def main() -> None:
    schnell = "--schnell" in sys.argv
    gctrace = "--gctrace" in sys.argv
    stufen = [int(a) for a in sys.argv[1:] if a.isdigit()] or [500, 1000, 2000]
    lang = KURZ if schnell else LANG

    if not EXE.exists():
        print(f"ABBRUCH: {EXE} fehlt. Erst bauen: go build -o ws-gateway.exe .")
        sys.exit(2)
    if not exe_aktuell():
        print("ABBRUCH: ws-gateway.exe ist aelter als eine .go-Datei. Neu bauen.")
        sys.exit(2)
    if not port_frei():
        print(f"ABBRUCH: Port {PORT} ist belegt - das laufende Gateway bitte beenden.")
        sys.exit(2)

    token = create_access_token({"user_id": str(USER)})
    dauer = sum(n / RATE + lang + 15 for n in stufen) / 60
    print(f"Stufen {stufen}, Rampe {RATE}/s, Messung nach {KURZ} s"
          f"{'' if schnell else f' und {LANG} s'}  (~{dauer:.0f} min)\n")

    ergebnisse = []
    for n in stufen:
        print(f"Stufe {n}:")
        r = await stufe(n, token, lang, gctrace)
        ergebnisse.append(r)
        if r["gc"]:
            g = r["gc"]
            print(f"  Letzte GC vor Ende (@{g['t']:.0f} s): lebender Heap {g['live']} MB, "
                  f"Ziel {g['ziel']} MB, Stacks {g['stacks']} MB "
                  f"-> {g['live'] * 1024 / n:.1f} + {g['stacks'] * 1024 / n:.1f} KB je Verbindung")
        elif gctrace:
            print("  Keine GC-Zeile im Log gefunden.")
        print(f"  TCP established: {r['tcp']}, vorzeitig getrennt: {r['vorzeitig']}, "
              f"429 (Client/Log): {r['429']}/{r['429_log']}, Fehler: {r['fehler'] or 'keine'}, "
              f"exit {r['exit']}")
        print(f"  Last nach {KURZ} s: {r['kurz_ws']:.1f} MB"
              f"{'' if schnell else f', nach {LANG} s: {r['lang_ws']:.1f} MB'}"
              f", Spitze {r['spitze']:.1f} MB\n")

    def tabelle(titel: str, leer: str, last: str) -> None:
        print(f"\n{titel}")
        print("| N | Leer (MB) | Last (MB) | Differenz | pro Verbindung | Grenzkosten |")
        print("|---|---|---|---|---|---|")
        vorher = None
        for r in ergebnisse:
            diff = r[last] - r[leer]
            grenz = "—"
            if vorher is not None:
                d_diff = diff - (vorher[last] - vorher[leer])
                grenz = f"{kb_pro(d_diff, r['n'] - vorher['n']):.1f} KB"
            print(f"| {r['n']} | {r[leer]:.1f} | {r[last]:.1f} | {diff:.1f} | "
                  f"{kb_pro(diff, r['n']):.1f} KB | {grenz} |")
            vorher = r

    tabelle(f"WorkingSet nach {KURZ} s (vergleichbar mit Phase 0: ~210 KB unter Python)",
            "leer_ws", "kurz_ws")
    if not schnell:
        tabelle(f"WorkingSet nach {LANG} s (Dauerbetrieb, nach mehreren Heartbeat-Takten)",
                "leer_ws", "lang_ws")
        tabelle(f"Privat nach {LANG} s (zugesagter Speicher)", "leer_pr", "lang_pr")

    sauber = all(r["offen"] == r["n"] and r["tcp"] == r["n"] and r["vorzeitig"] == 0
                 and not r["fehler"] and r["exit"] == 0 for r in ergebnisse)
    print(f"\nMessung {'sauber' if sauber else 'NICHT sauber - Zahlen oben pruefen'}.")
    print("Go-Logs: " + ", ".join(str(r["log"]) for r in ergebnisse))


asyncio.run(main())
