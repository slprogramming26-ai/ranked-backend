"""Lytir — Feature-Extraktion.

DAS IST DIE WICHTIGSTE DATEI DES GANZEN MODELLS.

Sie uebersetzt ein Paar (User, Post) in einen Vektor fester Laenge. Das Modell
sieht nie einen Post, nie ein JSON, nie einen String — es sieht ausschliesslich
diese Zahlenliste.

Regel Nummer eins: Training und Auslieferung muessen EXAKT dieselbe Funktion
benutzen. Wenn hier beim Training log1p(vote_count) steht und im Feed
vote_count/100, wird das Modell nicht kaputtgehen — es wird nur leise
schlechter, und der Grund ist praktisch nicht auffindbar. Deshalb liegt diese
Datei unter app/ (das Backend braucht sie zur Laufzeit) und training/
importiert sie von hier, nie umgekehrt.

Regel Nummer zwei: keine schweren Imports. Nur stdlib. Diese Datei laeuft im
Railway-Container mit, torch/numpy bleiben lokal.

Regel Nummer drei: keine DB-Zugriffe. Die Funktion bekommt fertige Rohwerte
und rechnet. Dadurch funktioniert sie identisch fuer ORM-Objekte (Feed) und
fuer simulierte oder aus CSV geladene Zeilen (Training).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from math import cos, log1p, pi, sin
from typing import List, Optional


# Version des Feature-Layouts. Bei JEDER Aenderung an FEATURE_NAMES oder an der
# Rechnung in build_features hochzaehlen und das Modell neu trainieren — ein
# altes Modell auf neuen Features ist Muell.
FEATURE_VERSION = 1

# Die Reihenfolge ist ein Vertrag zwischen Training und Inferenz.
# Niemals umsortieren, nur hinten anhaengen (und dann FEATURE_VERSION erhoehen).
FEATURE_NAMES: List[str] = [
    "log_age_hours",       # 0  wie alt ist der Post
    "log_vote_count",      # 1  Beliebtheit
    "log_comment_count",   # 2  Diskussion
    "has_image",           # 3
    "title_len",           # 4
    "content_len",         # 5
    "flag_none",           # 6  \
    "flag_engagement",     # 7   |  One-Hot von post.flag
    "flag_creativity",     # 8   |
    "flag_productivity",   # 9  /
    "i_follow_owner",      # 10 Beziehung User -> Autor
    "vibe_overlap",        # 11 0, 1 oder 2 gemeinsame Vibe-Faktoren
    "same_location",       # 12
    "log_author_xp",       # 13 \  Reputation des Autors
    "author_streak",       # 14 /
    "created_hour_sin",    # 15 \  Tageszeit zyklisch kodiert
    "created_hour_cos",    # 16 /
]

N_FEATURES = len(FEATURE_NAMES)

# Reihenfolge der One-Hot-Spalten fuer post.flag (None == kein Flag).
FLAGS: List[Optional[str]] = [None, "engagement", "creativity", "productivity"]


@dataclass
class FeatureInput:
    """Rohwerte fuer genau ein (User, Post)-Paar.

    Bewusst primitive Typen statt ORM-Objekte: dieselbe Struktur laesst sich
    aus der DB, aus dem Simulator oder aus einer CSV befuellen.
    """

    # --- Post ---
    age_hours: float
    vote_count: int
    comment_count: int
    has_image: bool
    title_len: int
    content_len: int
    flag: Optional[str]
    created_hour: int          # 0..23, Stunde in der der Post erstellt wurde

    # --- Beziehung User <-> Post/Autor ---
    i_follow_owner: bool
    vibe_overlap: int          # 0, 1 oder 2
    same_location: bool

    # --- Autor ---
    author_xp: int
    author_streak: int


def build_features(inp: FeatureInput) -> List[float]:
    """Rohwerte -> Vektor der Laenge N_FEATURES.

    Zwei Dinge passieren hier, und beide sind wichtiger als die Netzarchitektur:

    1. log1p auf alle Zaehler. Ein Post mit 1000 Votes ist nicht 1000x besser
       als einer mit einem Vote. Rohe Zaehler lassen ein paar Ausreisser den
       kompletten Gradienten dominieren.
    2. Alles grob in den Bereich 0..1 druecken. Ungleich skalierte Features
       sind der haeufigste Grund, warum ein Training "einfach nicht lernt".
    """
    flag_onehot = [1.0 if inp.flag == f else 0.0 for f in FLAGS]

    # Zyklische Kodierung: sonst waeren 23 Uhr und 0 Uhr maximal weit
    # voneinander entfernt, obwohl sie eine Stunde auseinanderliegen.
    angle = 2 * pi * (inp.created_hour % 24) / 24

    return [
        log1p(max(inp.age_hours, 0.0)) / 7.0,      # /7: ~30 Tage landen bei ~1
        log1p(max(inp.vote_count, 0)) / 7.0,
        log1p(max(inp.comment_count, 0)) / 5.0,
        1.0 if inp.has_image else 0.0,
        min(inp.title_len, 100) / 100.0,
        min(inp.content_len, 500) / 500.0,
        *flag_onehot,
        1.0 if inp.i_follow_owner else 0.0,
        min(max(inp.vibe_overlap, 0), 2) / 2.0,
        1.0 if inp.same_location else 0.0,
        log1p(max(inp.author_xp, 0)) / 10.0,
        min(max(inp.author_streak, 0), 30) / 30.0,
        sin(angle),
        cos(angle),
    ]


def vibe_overlap(user_vibes, author_vibes) -> int:
    """Anzahl gemeinsamer Vibe-Faktoren (0..2). None-Werte zaehlen nicht mit."""
    a = {v for v in user_vibes if v}
    b = {v for v in author_vibes if v}
    return len(a & b)


def features_from_orm(
    post,
    author,
    current_user,
    *,
    i_follow_owner: bool,
    comment_count: int = 0,
    now: Optional[datetime] = None,
) -> List[float]:
    """Adapter fuer den Serving-Pfad: SQLAlchemy-Objekte -> Feature-Vektor.

    Absichtlich ohne DB-Zugriff. i_follow_owner und comment_count muss der
    Aufrufer fuer alle Kandidaten in EINER Query vorab holen, sonst baut man
    sich beim Ranken von 200 Posts 400 Einzelqueries ein.
    """
    now = now or datetime.now(timezone.utc)

    created = post.created_at
    if created.tzinfo is None:                     # DB liefert je nach Treiber naiv
        created = created.replace(tzinfo=timezone.utc)

    return build_features(
        FeatureInput(
            age_hours=(now - created).total_seconds() / 3600.0,
            vote_count=post.vote_count or 0,
            comment_count=comment_count,
            has_image=bool(post.image_url),
            title_len=len(post.title or ""),
            content_len=len(post.content or ""),
            flag=post.flag,
            created_hour=created.hour,
            i_follow_owner=i_follow_owner,
            vibe_overlap=vibe_overlap(
                (current_user.vibe_factor_1, current_user.vibe_factor_2),
                (author.vibe_factor_1, author.vibe_factor_2),
            ),
            same_location=(
                post.location_id is not None
                and post.location_id == current_user.location_id
            ),
            author_xp=author.xp or 0,
            author_streak=author.streak_count or 0,
        )
    )
