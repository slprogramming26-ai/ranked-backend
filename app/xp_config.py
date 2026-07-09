"""XP- und Ligen-Konfiguration.

Gegenstueck zu ranking_config.py, aber fuer die ZWEITE Waehrung:
    - Daily Points (ranking_config)  -> resettet taeglich, Wettkampf
    - XP (hier)                      -> resettet nie, Progression/Level

WICHTIG: Wie viel XP etwas wert ist, entscheidet IMMER der Server. Der Client
liest XP/Liga nur aus der Profil-Antwort ab und rechnet nichts selbst.

Alle Werte hier sind bewusst an EINER Stelle, damit man am Spiel-Balancing
drehen kann, ohne die Endpoint-Logik anzufassen.
"""

# =========================================================
# XP-Quellen
# =========================================================

# Bewerter schliesst seine taegliche Swipe-Session ab -> belohnt das Mitmachen.
# Das ist der wichtigste Wert: er schliesst die einseitige Loop (der Bewerter
# bekommt endlich auch etwas).
XP_PER_SESSION = 50

# Bewerteter bekommt XP fuer JEDEN Punkt, den er an einem Tag erhaelt.
# 1:1 — wer heute 45 Daily Points bekommt, bekommt auch 45 XP dauerhaft gutgeschrieben.
XP_PER_POINT_RECEIVED = 1

# Streak-Meilensteine: einmalige Bonus-XP, wenn der Streak genau diese Laenge
# erreicht (7, 30, 100 Tage am Stueck geswiped). Wird beim Session-Abschluss geprueft.
STREAK_MILESTONE_BONUS = {
    7:   100,
    30:  500,
    100: 2000,
}

# Platzierungs-Boni am Tagesende (Phase 3 — Tagesende-Job). Hier schon definiert,
# damit alle XP-Werte an einem Ort stehen. Key = Platz-Grenze, Value = Bonus-XP.
# "Wer auf Platz 1 landet, bekommt 200; Top 3 (Platz 2-3) 100; Top 10 (Platz 4-10) 50."
PLACEMENT_BONUS = {
    1:  200,
    3:  100,
    10: 50,
}


# =========================================================
# Ligen (aus XP abgeleitet)
# =========================================================

# Liste von (XP-Schwelle, Liga-Name), AUFSTEIGEND sortiert.
# Ab dieser XP-Summe ist man in der Liga. Zum Balancen einfach Zahlen/Namen aendern.
LEAGUES = [
    (0,     "Bronze"),
    (500,   "Silber"),
    (1500,  "Gold"),
    (3500,  "Platin"),
    (7000,  "Diamant"),
    (12000, "Meister"),
    (20000, "Legende"),
]


def get_league(xp: int) -> dict:
    """Rechnet aus einer XP-Summe die aktuelle Liga + Fortschritt zur naechsten aus.

    Rueckgabe (alles was das Frontend fuer Ring + Fortschrittsbalken braucht):
        league          -> Name der aktuellen Liga  ("Gold")
        tier            -> Index 0..n der Liga       (fuer Ring-Farbe/Icon)
        next_league     -> Name der naechsten Liga   (None, wenn schon hoechste)
        xp_into_league  -> XP seit Beginn der aktuellen Liga  (Balken-Fuellstand)
        league_span     -> Breite der aktuellen Liga in XP    (Balken-Maximum)
        xp_for_next     -> noch fehlende XP bis zum Aufstieg   (0, wenn hoechste)
    """
    # Hoechste Liga finden, deren Schwelle die xp erreicht haben.
    # LEAGUES ist aufsteigend sortiert -> sobald die Schwelle groesser als xp ist,
    # koennen wir aufhoeren (alles danach ist erst recht zu hoch).
    current_index = 0
    for i, (threshold, _name) in enumerate(LEAGUES):
        if xp >= threshold:
            current_index = i
        else:
            break

    current_threshold, current_name = LEAGUES[current_index]

    # Gibt es ueberhaupt noch eine naechste Liga?
    is_highest = current_index + 1 >= len(LEAGUES)

    if is_highest:
        return {
            "league": current_name,
            "tier": current_index,
            "next_league": None,
            "xp_into_league": xp - current_threshold,
            "league_span": 0,
            "xp_for_next": 0,
        }

    next_threshold, next_name = LEAGUES[current_index + 1]
    return {
        "league": current_name,
        "tier": current_index,
        "next_league": next_name,
        "xp_into_league": xp - current_threshold,
        "league_span": next_threshold - current_threshold,
        "xp_for_next": next_threshold - xp,
    }
