"""Sichtpruefung der Label-Formel — kein Test, ein Blick drauf.

Aufruf aus dem Repo-Root:

    .\\venv\\Scripts\\python.exe -m training.label_check

Die Formel in labels.py haengt an fuenf geschaetzten Konstanten. Es gibt kein
"richtiges" Ergebnis, das man in einem Test festnageln koennte — nur ein
plausibles. Deshalb keine Assertions: Konstante in labels.py drehen, Skript
nochmal laufen lassen, Tabelle vergleichen.

Braucht weder Datenbank noch torch noch .env. Genau dafuer liegt die
Label-Politik in einer eigenen Datei: compute_label() ist reine Rechnung.
"""

from training.labels import compute_label


# Der Normalfall: gesehen, aber nichts gedrueckt. Nur hier spielt dwell ueberhaupt
# eine Rolle — bei einer Aktion steht das Label schon vorher fest.
KEINE_AKTION = dict(voted=False, opened_comments=False, shared=False, reported=False)

FAELLE = [
    ("200 Zeichen, kein Bild, vorbeigescrollt (1.5s)", dict(dwell_ms=1_500,   content_len=200, has_image=False)),
    ("200 Zeichen, kein Bild, ausgelesen (12s)",       dict(dwell_ms=12_000,  content_len=200, has_image=False)),
    ("200 Zeichen, kein Bild, haengengeblieben (35s)", dict(dwell_ms=35_000,  content_len=200, has_image=False)),
    ("50 Zeichen + Bild, kurz (2s)",                   dict(dwell_ms=2_000,   content_len=50,  has_image=True)),
    ("50 Zeichen + Bild, lange (18s)",                 dict(dwell_ms=18_000,  content_len=50,  has_image=True)),
    ("gekappt (180s), langer Post",                    dict(dwell_ms=180_000, content_len=500, has_image=False)),
]


def main() -> None:
    print("\nOhne Aktion — hier entscheidet allein die Verweildauer:")
    for name, werte in FAELLE:
        # ** kippt beide Dicts zu einem Aufruf zusammen. Geht nur, weil
        # compute_label keyword-only ist.
        label = compute_label(**KEINE_AKTION, **werte)
        print(f"  {label:.3f}   {name}")

    print("\nMit Aktion — die Verweildauer ist dann egal:")
    print(f"  {compute_label(voted=True, opened_comments=False, shared=False, reported=False, dwell_ms=1_200, content_len=200, has_image=False):.3f}   gevotet nach 1.2s")
    print(f"  {compute_label(voted=False, opened_comments=True, shared=False, reported=False, dwell_ms=60_000, content_len=200, has_image=False):.3f}   Kommentare geoeffnet")
    print(f"  {compute_label(voted=True, opened_comments=False, shared=False, reported=True, dwell_ms=60_000, content_len=200, has_image=False):.3f}   gevotet UND gemeldet (reported gewinnt)")
    print()


if __name__ == "__main__":
    main()
