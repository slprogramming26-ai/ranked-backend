"""Lytir — das Ranking-Modell.

Benannt nach dem nordischen Gott, den man befragt, bevor man handelt.

Aufbau:
    features.py   Uebersetzt (User, Post) in einen Zahlenvektor. Wird von
                  BEIDEN Seiten benutzt: vom Feed hier im Backend und vom
                  Trainingscode in training/.
    ranker.py     Laedt lytir.onnx und berechnet Scores. Optional — solange
                  kein Modell existiert, ist Lytir schlicht inaktiv.
    lytir.onnx    Das trainierte Modell (~30 KB). Kommt spaeter dazu.

Das Training selbst liegt bewusst NICHT hier, sondern in training/ ausserhalb
von app/ — torch soll nie im Railway-Container landen.
"""

from .features import FEATURE_NAMES, FEATURE_VERSION, N_FEATURES, FeatureInput, build_features

__all__ = [
    "FEATURE_NAMES",
    "FEATURE_VERSION",
    "N_FEATURES",
    "FeatureInput",
    "build_features",
]
