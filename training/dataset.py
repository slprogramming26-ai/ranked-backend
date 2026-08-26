"""Lytir — vom Rohdatensatz zum Trainingsbatch.

Diese Datei macht aus den Zeilen in feed_impressions das, was der Trainingsloop
braucht: eine Matrix X (ein Feature-Vektor pro Zeile) und einen Vektor y (ein
Zielwert pro Zeile).

Aufbau, in dieser Reihenfolge:
    1. load_rows()         Query gegen feed_impressions, inkl. Filter
    2. rows_to_arrays()    JSONB-Snapshot -> FeatureInput -> Feature-Vektor
    3. split_rows()        Aufteilung Training/Validierung, nach Session
    4. build_dataset()     alles zusammen -> vier Tensoren

Das y selbst wird hier nicht definiert, sondern in labels.py — das ist eine
Politik-Entscheidung und keine Pipeline-Mechanik.

Wie model.py liegt auch diese Datei bewusst ausserhalb von app/: sie importiert
torch, und torch soll nie im Railway-Container landen.
"""

from typing import List, NamedTuple, Tuple

import torch

from app import models
from app.database import SessionLocal
from app.lytir.features import (
    FEATURE_VERSION,
    FeatureInput,
    N_FEATURES,
    build_features,
)
from training.labels import compute_label


# --------------------------------------------------------------------------
# Teil 1: die Zeilen aus der Datenbank holen.
# --------------------------------------------------------------------------


def load_rows(db) -> List[models.FeedImpression]:
    """Alle brauchbaren Zeilen aus feed_impressions holen.

    Der Join auf posts dient NUR dem Filter — die Post-Spalten selbst brauchen
    wir nicht. Alles, was das Label und die Features wissen muessen, steht
    schon im JSONB-Snapshot der Impression-Zeile.
    """
    return (
        db.query(models.FeedImpression)
        .join(models.Post, models.Post.id == models.FeedImpression.post_id)
        .filter(
            # Filter 1: altes Feature-Layout raus. Bei FEATURE_VERSION = 2
            # stuenden in den alten Snapshots die Zahlen an anderer Stelle —
            # das Modell wuerde stillschweigend Unsinn lernen.
            models.FeedImpression.feature_version == FEATURE_VERSION,
            # Filter 2: eigene Posts raus. Man scrollt am eigenen Post anders
            # vorbei als an einem fremden; das ist kein Interesse, das ist
            # Selbstkontrolle.
            models.Post.owner_id != models.FeedImpression.user_id,
        )
        .all()
    )
    # Filter 3 gibt es bewusst NICHT: beide feed_variant ("local" und "for_you")
    # gehen in EIN Modell. Die Variante ist kein Feature des Posts, sondern der
    # Anzeigekontext — zwei getrennte Modelle wuerden die ohnehin knappen Daten
    # halbieren.


# --------------------------------------------------------------------------
# Teil 2: Snapshot -> Feature-Vektor -> X und y.
# --------------------------------------------------------------------------


def rows_to_arrays(
    rows: List[models.FeedImpression],
) -> Tuple[List[List[float]], List[float]]:
    """Impression-Zeilen -> Feature-Matrix X und Zielwerte y.

    Beide Listen entstehen in EINEM Durchlauf. Das ist Absicht: wuerde man
    erst alle X und danach in einer zweiten Schleife alle y bauen, koennte
    eine uebersprungene Zeile die beiden gegeneinander verschieben — und ab
    da lernt das Netz Rauschen, ohne dass irgendetwas kaputt aussieht.
    """
    X: List[List[float]] = []
    y: List[float] = []
    uebersprungen = 0

    for r in rows:
        try:
            # ** packt das Dict wieder in benannte Argumente aus. Das klappt
            # nur, weil der Endpunkt asdict(feature_input) geschrieben hat —
            # die JSON-Schluessel SIND die Feldnamen der Dataclass.
            inp = FeatureInput(**r.features)
        except TypeError:
            # Ein Schluessel fehlt oder ist zu viel. Nach Filter 1 sollte das
            # nicht vorkommen; wenn doch, ist die Zeile unbrauchbar.
            uebersprungen += 1
            continue

        # DIE zentrale Zeile der Datei: derselbe build_features(), das der Feed
        # spaeter aufruft. Keine zweite Rechnung, kein Training/Serving-Skew.
        X.append(build_features(inp))

        y.append(compute_label(
            # Verhalten des Users -> Spalten der Zeile.
            voted=r.voted,
            opened_comments=r.opened_comments,
            shared=r.shared,
            reported=r.reported,
            dwell_ms=r.dwell_ms,
            # Zustand des Posts DAMALS -> aus dem Snapshot, nicht aus posts.
            # Der Autor kann den Text seit der Impression geaendert haben.
            content_len=inp.content_len,
            has_image=inp.has_image,
        ))

    if uebersprungen:
        print(f"WARNUNG: {uebersprungen} von {len(rows)} Zeilen uebersprungen "
              f"(Snapshot passt nicht zu FeatureInput)")

    return X, y


# --------------------------------------------------------------------------
# Teil 3: aufteilen in Training und Validierung.
#
# Der Split passiert auf den ZEILEN, nicht auf X und y. Grund: die Kriterien,
# nach denen geteilt wird (feed_session_id, shown_at), stehen absichtlich
# nicht im Feature-Vektor — das Modell soll die Sitzungsnummer nicht kennen.
# Auf X liesse sich also gar nicht sinnvoll teilen.
# --------------------------------------------------------------------------

# Anteil der neuesten Sessions, der zur Validierung zurueckgehalten wird.
VAL_ANTEIL = 0.2


def split_rows(
    rows: List[models.FeedImpression],
    val_anteil: float = VAL_ANTEIL,
) -> Tuple[List[models.FeedImpression], List[models.FeedImpression]]:
    """Zeilen in Training und Validierung teilen — nach SESSION und Zeit.

    Nicht zufaellig zeilenweise: zwei Zeilen aus derselben Feed-Sitzung sind
    keine unabhaengigen Beispiele. Sie teilen den User, teils dieselben Posts
    und fast denselben Zeitpunkt. Landet eine im Training und eine in der
    Validierung, misst die Val-Loss nur noch Auswendiglernen — sie sieht
    hervorragend aus und sagt nichts ueber den echten Feed.

    Zusaetzlich nach Zeit sortiert (aelteste Sessions ins Training, neueste in
    die Validierung), weil genau das der spaetere Einsatz ist: auf
    Vergangenheit trainieren, Zukunft ranken. Nebenbei ist der Split dadurch
    reproduzierbar ohne random_state — es wird nichts gewuerfelt.
    """
    # Wann wurde jede Session zum ersten Mal gesehen?
    erste_sichtung = {}
    for r in rows:
        bisher = erste_sichtung.get(r.feed_session_id)
        if bisher is None or r.shown_at < bisher:
            erste_sichtung[r.feed_session_id] = r.shown_at

    # Sessions von alt nach neu. sorted() laeuft ueber die Schluessel des
    # Dicts, key= sagt ihm, wonach er vergleichen soll.
    sessions = sorted(erste_sichtung, key=lambda s: erste_sichtung[s])

    # Mindestens eine Session zurueckhalten — aber nur, wenn ueberhaupt mehr
    # als eine da ist. Bei einer einzigen Session gibt es nichts zu validieren.
    n_val = max(1, round(len(sessions) * val_anteil)) if len(sessions) > 1 else 0
    val_sessions = set(sessions[-n_val:]) if n_val else set()

    train = [r for r in rows if r.feed_session_id not in val_sessions]
    val = [r for r in rows if r.feed_session_id in val_sessions]

    if not val:
        print(f"WARNUNG: nur {len(sessions)} Session(s) — keine Validierung moeglich")

    return train, val


# --------------------------------------------------------------------------
# Teil 4: Listen -> Tensoren. Ab hier ist es torch-Futter.
# --------------------------------------------------------------------------


class Dataset(NamedTuple):
    """Die vier Tensoren, die der Trainingsloop braucht.

    Bewusst ein NamedTuple statt vier lose Rueckgabewerte: bei
    "return X_train, y_train, X_val, y_val" reicht ein vertauschtes Paar beim
    Entpacken, und du trainierst auf dem Validierungsset. Das wirft keinen
    Fehler — die Formen passen ja. Mit ds.X_train kann das nicht passieren.
    """

    X_train: torch.Tensor
    y_train: torch.Tensor
    X_val: torch.Tensor
    y_val: torch.Tensor


def _tensor_x(X: List[List[float]]) -> torch.Tensor:
    """Liste von Listen -> Tensor der Form (N, N_FEATURES).

    Das reshape ist fuer den leeren Fall da: torch.tensor([]) hat die Form
    (0,), nicht (0, 17). Ohne reshape wuerde jeder spaetere Zugriff auf die
    zweite Dimension knallen — und genau das ist bei uns gerade der
    Normalfall, weil das Validierungsset leer ist.
    """
    return torch.tensor(X, dtype=torch.float32).reshape(-1, N_FEATURES)


def _tensor_y(y: List[float]) -> torch.Tensor:
    """Liste -> Tensor der Form (N,).

    Die Form ist kein Zufall: model.forward() macht am Ende .squeeze(-1) und
    liefert (N,). BCEWithLogitsLoss verlangt, dass Ausgabe und Target exakt
    dieselbe Form haben — (N, 1) gibt einen ValueError.

    float32, NICHT long. Zwei Gruende, und der zweite ist der wichtige:
      1. BCEWithLogitsLoss will Float-Targets.
      2. torch.tensor([0.38], dtype=torch.long) ergibt tensor([0]) — still,
         ohne Warnung. Genau die weichen Labels, um die es in labels.py geht,
         waeren damit weg.
    """
    return torch.tensor(y, dtype=torch.float32)


def build_dataset(db) -> Dataset:
    """Der komplette Weg: Datenbank -> vier fertige Tensoren."""
    rows = load_rows(db)
    train_rows, val_rows = split_rows(rows)

    # rows_to_arrays laeuft je Haelfte einmal — die Funktion selbst weiss vom
    # Split nichts und muss dafuer nicht angefasst werden.
    X_train, y_train = rows_to_arrays(train_rows)
    X_val, y_val = rows_to_arrays(val_rows)

    return Dataset(
        X_train=_tensor_x(X_train),
        y_train=_tensor_y(y_train),
        X_val=_tensor_x(X_val),
        y_val=_tensor_y(y_val),
    )


if __name__ == "__main__":
    db = SessionLocal()
    try:
        ds = build_dataset(db)
    finally:
        db.close()

    print()
    print(f"  X_train {tuple(ds.X_train.shape)}  {ds.X_train.dtype}")
    print(f"  y_train {tuple(ds.y_train.shape)}  {ds.y_train.dtype}")
    print(f"  X_val   {tuple(ds.X_val.shape)}  {ds.X_val.dtype}")
    print(f"  y_val   {tuple(ds.y_val.shape)}  {ds.y_val.dtype}")
    print()
    print("  y_train:", [round(v, 3) for v in ds.y_train.tolist()])
    print()
