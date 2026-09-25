"""Lytir — der Ranker. Rechnet das trainierte Netz ohne torch aus.

Die Gewichte kommen aus lytir.json (erzeugt von training/export.py).
Die Architektur steht NICHT in der Datei, sondern hier im Code:
Linear -> ReLU -> Linear -> ReLU -> Linear, wie in training/model.py.
Aendert sich dort etwas, muss es hier mit.

Regeln wie in features.py: nur stdlib, keine DB.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple
from operator import mul

from app.lytir.features import FEATURE_VERSION





# Liegt erst hier, wenn ein ECHTES Modell trainiert ist — von Hand aus
# training/data/ kopiert und committet. Bis dahin fehlt sie absichtlich.
GEWICHTE = Path(__file__).parent / "lytir.json"

# Eine Schicht = (W, b). W hat eine Zeile pro Neuron.
Schicht = Tuple[List[List[float]], List[float]]


@lru_cache(maxsize=1)
def _modell_laden() -> Optional[List[Schicht]]:
    """lytir.json -> Liste der Schichten, oder None wenn es kein Modell gibt."""
    if not GEWICHTE.exists():
        return None

    daten = json.loads(GEWICHTE.read_text(encoding="utf-8"))

    if daten["feature_version"] != FEATURE_VERSION:
        print(
            f"Lytir: Modell hat FEATURE_VERSION {daten['feature_version']}, "
            f"der Code {FEATURE_VERSION} — Modell wird NICHT benutzt."
        )
        return None

    return [(s["W"], s["b"]) for s in daten["schichten"]]


def _logit(schichten: List[Schicht], x: List[float]) -> float:
    """Ein Feature-Vektor durchs Netz -> roher Logit (wie forward() in model.py)."""
    letzte = len(schichten) - 1

    for i, (W, b) in enumerate(schichten):
        # Pro Neuron: seine Gewichts-Zeile mal die Eingabe, aufsummiert, plus Bias.
        x = [sum(map(mul, zeile, x)) + b_j for zeile, b_j in zip(W, b)]

        # ReLU zwischen den Schichten, aber NICHT nach der letzten.
        if i < letzte:
            x = [max(v, 0.0) for v in x]

    return x[0]


def scores(vektoren: List[List[float]]) -> Optional[List[float]]:
    """Ein Logit pro Feature-Vektor, gleiche Reihenfolge. None = kein Modell."""
    schichten = _modell_laden()
    if schichten is None:
        return None
    return [_logit(schichten, x) for x in vektoren]

