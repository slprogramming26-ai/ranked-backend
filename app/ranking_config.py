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


# --- Feed-Ranking: Gewichte fuer die Score-Sortierung in get_posts ---
# score = FEED_VOTE_WEIGHT * votes
#       - FEED_AGE_PENALTY_PER_HOUR * alter_in_stunden
#       + FEED_FOLLOW_BONUS  (wenn ich dem Autor folge)
#       + FEED_VIBE_BONUS    (wenn Autor und ich einen Vibe-Faktor teilen)

FEED_VOTE_WEIGHT = 1
FEED_AGE_PENALTY_PER_HOUR = 1
FEED_FOLLOW_BONUS = 50
FEED_VIBE_BONUS = 20


