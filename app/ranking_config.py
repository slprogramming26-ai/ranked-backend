"""Ranking-Konfiguration: Punktewerte fuer Swipes.

WICHTIG: Diese Werte berechnet IMMER der Server. Der Client kennt sie nie
und schickt nur die Richtung (direction) eines Swipes — wie viel das wert ist,
entscheidet ausschliesslich der Server anhand von post.flag.

Aufbau:
    SWIPE_POINTS[flag]["right"]  -> Punkte fuer einen Rechts-Swipe (Cool)
    SWIPE_POINTS[flag]["left"]   -> Punkte fuer einen Links-Swipe (Nicht cool)

Ein gesetztes flag ("engagement"/"creativity"/"productivity") ist ein
Multiplikator: geflaggte Posts bringen mehr Punkte als ungeflaggte (None).

KEIN Downvoting: auch ein Links-Swipe gibt immer Punkte (nur deutlich
weniger). Hochladen wird grundsaetzlich belohnt, nie bestraft — alle Werte
sind positiv.
"""

SWIPE_POINTS = {
    None:           {"right": 3, "left": 1},
    "engagement":   {"right": 5, "left": 2},
    "creativity":   {"right": 5, "left": 2},
    "productivity": {"right": 5, "left": 2},
}
