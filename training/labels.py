"""Lytir — die Label-Politik.

Was ist ein "guter" Post? Diese Datei ist die einzige Antwort darauf. Sie
uebersetzt das beobachtete Verhalten EINER Impression in einen Zielwert
zwischen 0 und 1 — das y, auf das trainiert wird.

Bewusst getrennt von dataset.py, aus zwei Gruenden:

  1. Es ist eine Politik, keine Mechanik. An den fuenf Konstanten unten wirst
     du noch oft drehen (Kalibrierung), waehrend die Pipeline in dataset.py
     unveraendert bleibt. Getrennte Dateien machen sichtbar, was Meinung ist
     und was Handwerk.
  2. Reine Arithmetik: keine Datenbank, kein torch, nur stdlib. Deshalb laesst
     sich label_check.py in Millisekunden starten, statt erst torch zu laden.
"""

from math import log1p


# --------------------------------------------------------------------------
# Die Stellschrauben der Label-Formel.
#
# Alle fuenf Zahlen sind geschaetzt, nicht gemessen — und das ist in Ordnung,
# weil das Label ERST HIER berechnet wird und nicht schon beim Einsammeln.
# In feed_impressions stehen die Rohwerte. Du kannst diese Konstanten also
# beliebig oft drehen und neu trainieren, ohne eine einzige Impression neu
# sammeln zu muessen.
# --------------------------------------------------------------------------

# Obergrenze fuer reines Lesen ohne Aktion. Bewusst < 1.0: ein echter Vote soll
# immer schwerer wiegen als "lange draufgeschaut".
DWELL_MAX_LABEL = 0.6

# Ueberfliegendes Lesen. Kein Literaturstudium — Feed-Tempo.
READ_CHARS_PER_SEC = 20.0

# Grundzeit fuers Wahrnehmen, unabhaengig von der Laenge: hinschauen, erfassen,
# entscheiden ob es interessiert.
BASE_ATTENTION_S = 1.5

# Ein Bild kostet zusaetzliche Aufmerksamkeit, die in content_len nicht steckt.
IMAGE_ATTENTION_S = 2.0

# Ab dem Wievielfachen der erwarteten Lesezeit ist die Skala voll. Bei 3.0 gilt:
# dreifache Lesezeit = maximale Wertung, laenger bringt nichts mehr.
DWELL_SATURATION_RATIO = 3.0


def compute_label(
    *,
    voted: bool,
    opened_comments: bool,
    shared: bool,
    reported: bool,
    dwell_ms: int,
    content_len: int,
    has_image: bool,
) -> float:
    """Die Signale EINER Impression -> ein Zielwert zwischen 0.0 und 1.0.

    Kein 0-oder-1: BCEWithLogitsLoss akzeptiert jeden Zielwert im Intervall
    [0, 1]. Bei y = 0.3 zieht der Gradient das Modell eben auf 30 % statt auf
    0 oder 100. Das ist kein Trick, sondern die normale Definition der
    Cross-Entropy — und der einzige Weg, "hat gelesen, aber nicht reagiert"
    ueberhaupt ausdruecken zu koennen.

    Warum dwell NICHT als Sample-Weight taugt: ein Gewicht sagt nicht "dieses
    Beispiel ist positiver", sondern "nimm dieses Beispiel wichtiger" — es
    verstaerkt das Label, das schon dasteht. Langes Lesen ohne Aktion haette
    dann Label 0 bei hohem Gewicht und waere damit ein besonders STARKES
    Negativbeispiel. Exakt das Gegenteil der Absicht.

    Alle Parameter keyword-only: vier davon sind Booleans, und ein vertauschtes
    Paar wuerde keinen Fehler werfen, sondern still falsche Labels erzeugen.
    """
    # Reihenfolge ist Absicht, nicht Zufall: man kann voten UND danach melden.
    # Ohne feste Praezedenz haenge das Label davon ab, wie die ifs sortiert sind.
    if reported:
        return 0.0

    # Eine echte Aktion ist das staerkste Signal, das es gibt. Die drei bewusst
    # als ODER und ohne reported: ein "reacted" ueber alle vier wuerde dem
    # Modell beibringen, gemeldete Posts oefter auszuspielen.
    if voted or opened_comments or shared:
        return 1.0

    # --- Ab hier: gesehen, gelesen, aber nichts gedrueckt --------------------

    # Wie lange braucht ein Mensch fuer DIESEN Post ungefaehr.
    # Kann nie 0 werden (BASE_ATTENTION_S > 0) -> die Division unten ist sicher.
    erwartet_s = (
        BASE_ATTENTION_S
        + max(content_len, 0) / READ_CHARS_PER_SEC
        + (IMAGE_ATTENTION_S if has_image else 0.0)
    )

    # DER entscheidende Schritt: nicht die rohe Verweildauer, sondern die
    # Verweildauer IM VERHAELTNIS zur erwarteten Lesezeit.
    #
    # Mit roher Zeit wuerde das Netz die billigste Regel lernen, die zu den
    # Daten passt — "viel Text = gut". content_len ist Feature Nr. 5, die
    # Abkuerzung liegt also direkt vor seiner Nase. Das waere keine
    # Qualitaetsaussage, sondern gemessene Lesezeit, und im Trainingsverlauf
    # sieht es trotzdem gut aus. Durch die Division faellt die Laenge raus:
    # uebrig bleibt "laenger geschaut als der Post hergibt".
    ratio = max(dwell_ms, 0) / 1000.0 / erwartet_s

    # log statt linear: der Unterschied zwischen einfacher und doppelter
    # Lesezeit ist echtes Interesse, der zwischen 8x und 9x ist Rauschen.
    # min(..., 1.0) deckelt bei DWELL_SATURATION_RATIO — auch die gekappten
    # 180000-ms-Zeilen landen damit sauber bei 1.0 statt darueber.
    dwell_norm = min(log1p(ratio) / log1p(DWELL_SATURATION_RATIO), 1.0)

    return DWELL_MAX_LABEL * dwell_norm
