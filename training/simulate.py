"""Lytir — Simulator fuer Trainingsdaten.

ACHTUNG, einmal laut: die Zeilen hier sind ERFUNDEN. Ein Modell, das nur auf
ihnen trainiert wurde, kann genau eine Sache — die Regel nachahmen, die weiter
unten in dieser Datei steht. Ueber echte Nutzer weiss es nichts.

Wofuer sie trotzdem da sind: train.py, der ONNX-Export und ranker.py brauchen
Daten mit der richtigen FORM, nicht mit der richtigen Wahrheit. Damit laesst
sich die komplette Kette heute fertigbauen und debuggen. Kommen spaeter echte
Impressions dazu, wird nur die Quelle getauscht — kein Code aendert sich.

Diese Zeilen gehen NIE in die Datenbank. feed_impressions hat keine Spalte, die
echt von erfunden trennt; einmal drin, waeren die echten Daten fuer immer
unbrauchbar. Deshalb JSONL-Datei unter training/data/.
"""

import random

from app.lytir.features import FLAGS, FeatureInput

from math import exp, log1p

from typing import NamedTuple

from training.labels import expected_read_seconds

import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Dict, List, NamedTuple


import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.lytir.features import FEATURE_NAMES, FLAGS, FeatureInput, build_features
from training.labels import compute_label, expected_read_seconds






def random_feature_input(rng: random.Random) -> FeatureInput:
    """Ein zufaelliger (User, Post)-Kontext.

    Wichtigste Anforderung: JEDE Spalte muss variieren. In den echten Daten
    waren 12 von 17 Feature-Spalten konstant — aus einer konstanten Spalte
    kann ein Netz nichts lernen, sie ist dann nur teurer Ballast.
    """
    vote_count = int(rng.expovariate(1 / 8.0))

    return FeatureInput(
        # expovariate: viele frische Posts, wenige alte. Der Feed zeigt
        # ohnehin nur 30 Tage (= 720 h), daher der Deckel.
        age_hours=min(rng.expovariate(1 / 12.0), 720.0),
        vote_count=vote_count,
        # Kommentare sind seltener als Votes und haengen an ihnen dran.
        comment_count=int(vote_count * rng.uniform(0.0, 0.4)),
        has_image=rng.random() < 0.7,
        title_len=rng.randint(5, 80),
        # 30 % reine Bild-Posts, der Rest mit Text. Genau diese Mischung
        # fehlte in den echten Daten komplett.
        content_len=0 if rng.random() < 0.3 else rng.randint(20, 500),
        flag=rng.choice(FLAGS),
        created_hour=rng.randrange(24),
        i_follow_owner=rng.random() < 0.3,
        vibe_overlap=rng.randint(0, 2),
        same_location=rng.random() < 0.25,
        author_xp=rng.randint(0, 5000),
        author_streak=rng.randint(0, 30),
    )


# --------------------------------------------------------------------------
# Die Lehrer-Regel.
#
# DAS HIER IST DIE OBERGRENZE DES MODELLS. Was in dieser Funktion nicht steht,
# kann das Netz aus simulierten Daten unmoeglich lernen — es gibt keine
# verborgene Wahrheit, die es noch entdecken koennte. Die Regel IST die
# Wahrheit. Deshalb beweist ein gutes Ergebnis hier nur, dass die Kette laeuft.
#
# Zwei Dinge sind Absicht:
#   1. Eine ECHTE INTERAKTION (Votes x Frische). Waere die Regel eine reine
#      Summe gewichteter Features, koennte ein einzelnes nn.Linear sie perfekt
#      nachbilden — die ReLU-Schichten haetten nichts zu tun, und "das
#      Training laeuft" haette nichts bewiesen.
#   2. Features, die GAR NICHT vorkommen: title_len, content_len,
#      created_hour, author_streak, flag. Die muss das Netz als Rauschen
#      erkennen und ignorieren. Im Echtbetrieb ist das der Normalfall.
# --------------------------------------------------------------------------

# Grundniveau, negativ: die meisten Posts im Feed interessieren schlicht nicht.
# Diese eine Zahl steuert, wie viele Positivbeispiele entstehen.
GRUNDNIVEAU = -3.5


def interest_score(inp: FeatureInput) -> float:
    """Wie wahrscheinlich reagiert der Fake-User auf diesen Post? 0.0 bis 1.0."""
    # max(0, ...) ist eine ReLU von Hand: 1.0 bei einem brandneuen Post,
    # linear fallend, ab 48 Stunden konstant 0.
    frische = max(0.0, 1.0 - inp.age_hours / 48.0)
    beliebtheit = log1p(inp.vote_count) / 7.0

    score = GRUNDNIVEAU

    # --- Beziehung zum Autor: das staerkste Signal ---
    score += 2.0 if inp.i_follow_owner else 0.0
    score += 1.2 * inp.vibe_overlap / 2.0
    score += 0.8 if inp.same_location else 0.0

    # --- DIE Interaktion: Votes zaehlen nur, solange der Post frisch ist ---
    score += 2.5 * beliebtheit * frische

    # --- Kleinkram ---
    score += 0.4 if inp.has_image else 0.0
    score += 0.6 * log1p(inp.comment_count) / 5.0
    score += 0.5 * log1p(inp.author_xp) / 10.0
    score -= 1.0 - frische

    # Sigmoid: quetscht die offene Skala von score auf 0..1 zusammen.
    return 1.0 / (1.0 + exp(-score))


# --------------------------------------------------------------------------
# Teil 3: aus der Wahrscheinlichkeit wird beobachtbares Verhalten.
#
# Wichtig: hier entstehen ROHWERTE, keine Labels. Der Simulator ahmt einen
# Menschen nach, nicht labels.py. Was daraus fuer ein y wird, entscheidet
# spaeter allein compute_label() — sonst haetten wir die Label-Politik zweimal.
# --------------------------------------------------------------------------

# Melden ist selten und haengt NICHT am Interesse. Bewusst unabhaengig von den
# drei positiven Signalen: so entstehen auch Zeilen mit voted UND reported, und
# genau die pruefen die Vorrang-Regel in compute_label().
MELDE_WAHRSCHEINLICHKEIT = 0.005

# Grenzen wie in der Wirklichkeit: darunter sendet der Client gar nicht,
# genau der Maximalwert heisst in der DB "gekappt", nicht "gemessen".
DWELL_MIN_MS = 1000
DWELL_MAX_MS = 180000


class Behavior(NamedTuple):
    """Was der Fake-User mit einem Post gemacht hat.

    NamedTuple aus demselben Grund wie Dataset in dataset.py: vier der fuenf
    Felder sind Booleans. Ein vertauschtes Paar beim Entpacken wuerde keinen
    Fehler werfen, sondern still falsche Trainingsdaten erzeugen.
    """

    voted: bool
    opened_comments: bool
    shared: bool
    reported: bool
    dwell_ms: int


def simulate_behavior(
    inp: FeatureInput, p: float, rng: random.Random
) -> Behavior:
    """Interesse (0..1) -> was tatsaechlich beobachtet wurde."""
    # Je interessanter, desto wahrscheinlicher jede Aktion — aber mit sehr
    # unterschiedlichen Grundraten. Voten ist billig, teilen ist teuer.
    voted = rng.random() < p * 0.8
    opened_comments = inp.comment_count > 0 and rng.random() < p * 0.3
    shared = rng.random() < p * 0.05
    reported = rng.random() < MELDE_WAHRSCHEINLICHKEIT

    # --- Verweildauer --------------------------------------------------
    erwartet_s = expected_read_seconds(
        content_len=inp.content_len, has_image=inp.has_image
    )

    # Interesse steuert, wie lange geschaut wird, IM VERHAELTNIS zur
    # erwarteten Lesezeit: bei p = 0 knapp ein Drittel davon (weggescrollt),
    # bei p = 1 das Doppelte.
    ziel_ratio = 0.3 + 1.7 * p

    # lognormvariate streut MULTIPLIKATIV um diesen Zielwert: halb so lang und
    # doppelt so lang sind gleich wahrscheinlich, negativ wird es nie.
    ratio = ziel_ratio * rng.lognormvariate(0.0, 0.4)

    dwell_ms = int(erwartet_s * ratio * 1000)

    return Behavior(
        voted=voted,
        opened_comments=opened_comments,
        shared=shared,
        reported=reported,
        dwell_ms=max(DWELL_MIN_MS, min(dwell_ms, DWELL_MAX_MS)),
    )


# --------------------------------------------------------------------------
# Teil 4: Sitzungen bauen.
#
# Warum ueberhaupt Sitzungen und nicht einfach N lose Zeilen? Weil
# split_rows() in dataset.py nach SESSION und Zeit teilt. Haette jede Zeile
# ihre eigene feed_session_id, waere der Split wieder zeilenweise — genau das,
# was dort bewusst vermieden wird. Der Simulator muss die Struktur
# nachbilden, gegen die spaeter validiert wird, sonst testet die Attrappe den
# interessantesten Teil der Pipeline nicht.
# --------------------------------------------------------------------------

# Wie viele Posts scrollt ein User in einer Sitzung durch.
POSTS_PRO_SESSION = (8, 30)

# Pause zwischen zwei Sitzungen: 20 Minuten bis 2 Tage.
PAUSE_ZWISCHEN_SESSIONS_S = (1200, 172800)

FEED_VARIANTEN = ["for_you", "local"]


def simulate_session(rng: random.Random, start: datetime) -> List[Dict]:
    """Eine Feed-Sitzung: mehrere Posts, fortlaufende Zeit, EINE Session-ID."""
    session_id = str(uuid.uuid4())
    variant = rng.choice(FEED_VARIANTEN)
    shown_at = start
    rows: List[Dict] = []

    for position in range(rng.randint(*POSTS_PRO_SESSION)):
        inp = random_feature_input(rng)
        b = simulate_behavior(inp, interest_score(inp), rng)

        rows.append({
            "feed_session_id": session_id,
            "feed_variant": variant,
            "position": position,
            # JSON kennt kein datetime -> ISO-String. Block 5 parst zurueck.
            "shown_at": shown_at.isoformat(),
            "dwell_ms": b.dwell_ms,
            "voted": b.voted,
            "opened_comments": b.opened_comments,
            "shared": b.shared,
            "reported": b.reported,
            # asdict() erzeugt genau das Dict, das auch impression.py in die
            # JSONB-Spalte schreibt: die Schluessel SIND die Feldnamen von
            # FeatureInput. Darauf verlaesst sich rows_to_arrays() mit seinem
            # FeatureInput(**r.features).
            "features": asdict(inp),
        })

        # Die Uhr laeuft weiter, waehrend gescrollt wird.
        shown_at += timedelta(milliseconds=b.dwell_ms)

    return rows


def simulate_rows(
    n_sessions: int, rng: random.Random, ende: datetime
) -> List[Dict]:
    """Mehrere Sitzungen, rueckwaerts von 'ende' in die Vergangenheit gelegt.

    Rueckwaerts, damit die neuesten Zeilen immer direkt an 'ende' liegen —
    unabhaengig davon, wie viele Sitzungen erzeugt werden. Die Reihenfolge in
    der Liste ist dabei egal: split_rows() sortiert selbst nach der ersten
    Sichtung jeder Session.
    """
    alle: List[Dict] = []
    start = ende

    for _ in range(n_sessions):
        start -= timedelta(seconds=rng.randint(*PAUSE_ZWISCHEN_SESSIONS_S))
        alle.extend(simulate_session(rng, start))

    return alle


# --------------------------------------------------------------------------
# Teil 5: Datei schreiben und nachsehen, ob der Satz etwas taugt.
# --------------------------------------------------------------------------

SEED = 42
N_SESSIONS = 200

# Path(__file__).parent ist IMMER der training/-Ordner, egal aus welchem
# Verzeichnis du startest. Ein relatives "data/sim.jsonl" wuerde dagegen dort
# landen, wo die Konsole gerade steht.
AUSGABE = Path(__file__).parent / "data" / "sim.jsonl"


def write_jsonl(rows: List[Dict], pfad: Path) -> None:
    """Eine Zeile JSON pro Datensatz.

    JSONL statt einer grossen JSON-Liste: man kann zeilenweise lesen, ohne
    die ganze Datei in den Speicher zu holen, und spaeter einfach hinten
    anhaengen. Bei einer Liste muesste man dafuer die schliessende Klammer
    suchen und ueberschreiben.

    encoding="utf-8" ausdruecklich: unter Windows waere die Vorgabe cp1252,
    und die Datei waere auf jedem anderen System kaputt.
    """
    pfad.parent.mkdir(parents=True, exist_ok=True)

    with pfad.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def print_statistik(rows: List[Dict]) -> None:
    """Zwei Fragen, die ueber Brauchbarkeit entscheiden."""
    labels = [
        compute_label(
            voted=r["voted"],
            opened_comments=r["opened_comments"],
            shared=r["shared"],
            reported=r["reported"],
            dwell_ms=r["dwell_ms"],
            content_len=r["features"]["content_len"],
            has_image=r["features"]["has_image"],
        )
        for r in rows
    ]
    sortiert = sorted(labels)

    print(f"  Zeilen:   {len(rows)}")
    print(f"  Sessions: {len({r['feed_session_id'] for r in rows})}")
    print()
    print("  Label-Verteilung")
    print(f"    Median:   {sortiert[len(sortiert) // 2]:.3f}")
    print(f"    y == 1.0: {sum(1 for y in labels if y == 1.0) / len(labels):.1%}")
    print(f"    y == 0.0: {sum(1 for y in labels if y == 0.0) / len(labels):.1%}")
    print()

    # Frage zwei — daran ist es beim letzten Mal gescheitert: von 17 Spalten
    # waren 12 konstant. Eine konstante Spalte traegt null Information, das
    # Netz schleppt sie nur mit.
    X = [build_features(FeatureInput(**r["features"])) for r in rows]

    konstant = []
    for i, name in enumerate(FEATURE_NAMES):
        werte = {round(zeile[i], 6) for zeile in X}
        if len(werte) == 1:
            konstant.append(name)

    if konstant:
        print(f"  WARNUNG: konstante Spalten: {', '.join(konstant)}")
    else:
        print(f"  Alle {len(FEATURE_NAMES)} Feature-Spalten variieren.")


if __name__ == "__main__":
    rng = random.Random(SEED)
    rows = simulate_rows(N_SESSIONS, rng, datetime.now(timezone.utc))
    write_jsonl(rows, AUSGABE)

    print()
    print(f"  geschrieben: {AUSGABE}")
    print()
    print_statistik(rows)
    print()


