# Lytir

**Code:** `app/lytir/features.py` (Features), `app/lytir/ranker.py` (Rechnen im Feed),
`app/routers/impression.py` (`POST /impressions/`), `app/feed.py` (`_rerank`), Schalter in
`app/ranking_config.py`, Training in `training/` (außerhalb von `app/`)
**Tests:** `scripts/rerank_vergleich.py` (V10), `training/label_check.py` (Sichtprüfung)
**Verwandt:** [feed.md](feed.md)

## Worum es geht

Lytir ist ein kleines neuronales Netz, das den [Feed](feed.md) persönlicher sortieren soll. Es
bekommt für jedes Paar (ich, Post) 17 Zahlen und gibt eine Zahl zurück: wie gut der Post zu mir
passt. Der Feed holt 150 Kandidaten, Lytir sortiert sie um.

**Stand heute:**

| Teil | Stand |
|---|---|
| Impressions sammeln | Endpoint **läuft** (Beta-Datensammlung) |
| Features, Label, Training, Export | fertig, aber nur mit **simulierten** Daten erprobt |
| Ranker im Backend (`ranker.py`) | fertig, rechnet ohne torch |
| Rerank im Feed | fertig, aber **aus** (`LYTIR_RERANK_ENABLED = False`), kein Modell in `app/lytir/` |

Pausiert seit 2026-09-25, bis genug echte Impressions da sind. Ein Modell aus simulierten Daten
geht **nie** live: Es kann nur die Regel nachahmen, mit der die Daten erfunden wurden.

## Die Kette

```
 App                    Backend                      lokal (training/, mit torch)
  │                                                   │
  │ POST /impressions/  → feed_impressions             │
  │  (was wurde gezeigt,   (Rohwerte-Snapshot  ──────→ │ dataset.py: Zeilen laden, Features bauen
  │   wie lange, Like?)     pro Post)                  │ labels.py:  Verhalten → Zielwert 0..1
  │                                                   │ train.py:   Netz trainieren → lytir.pt
  │                                                   │ export.py:  → lytir.json (+ .onnx)
  │                                                   │
  │                      app/lytir/lytir.json  ←───────┘ von Hand kopieren + committen
  │                        │
  │ GET /posts/  ──→ feed.py: 150 Kandidaten → ranker.py → sortiert
```

Die eine Regel, die alles zusammenhält: **Training und Feed benutzen dieselbe Funktion
`build_features`.** Sie liegt in `app/lytir/features.py`. `training/` importiert sie von dort,
nie umgekehrt.

## 1. Impressions sammeln (`POST /impressions/`)

Ein Like sagt „gefällt mir“. Was dem Modell sonst fehlt, ist das Gegenteil: **gesehen und
weitergescrollt.** Ohne diese Zeilen gäbe es nur positive Beispiele, und das Netz lernt nichts
Unterscheidbares. Deshalb meldet die App, welche Posts wirklich auf dem Bildschirm waren.

### Nur mit Einwilligung

Gespeichert wird nur, wenn `users.impressions_enabled` an ist. Standard ist **aus** (Opt-in,
Art. 6 Abs. 1 lit. a DSGVO). Was jemand wie lange angeschaut hat, ist ein feines
Verhaltensprotokoll, das für den Betrieb der App nicht nötig ist, nur fürs Training.

- **Schalter aus:** `POST /impressions/` antwortet sofort **204** und schreibt nichts. Kein
  Fehlercode, weil die App fire-and-forget schickt und ihn ohnehin ignorieren würde. Die Prüfung
  steht ganz oben, deshalb fallen auch die drei Abfragen für den Snapshot weg.
- **Ausschalten = Widerruf:** `PUT /users/` mit `{"impressions_enabled": false}` löscht in
  **derselben Transaktion** alle `feed_impressions` dieses Users (Art. 7 Abs. 3, Art. 17 Abs. 1
  lit. b). Gelöscht wird nur bei echtem `false`. Fehlt das Feld oder ist es `null`, passiert nichts.
  Sonst würde jede Änderung der Bio die Daten löschen.
- **Lesen:** `GET /users/` liefert `impressions_enabled`, nur fürs eigene Profil.
- **Randfall:** Ein Batch, der die Prüfung schon hinter sich hat, während in denselben
  Millisekunden das Ausschalten committet, landet noch in der Tabelle. Ab dem nächsten Batch ist
  Schluss. Absichern ginge nur mit einer Sperre auf die User-Zeile. Das ist es nicht wert.

Der Schalter ist unabhängig von `personalized_feed` (siehe
[feed.md, Abschnitt 7](feed.md#7-personalisierung-abschalten-personalized_feed)). Lytir liest beim
Sortieren keine Impressions, sie sind nur Trainingsdaten.

### Was die App schickt

Alle 10 Sekunden oder wenn in der App ein Auslöser feuert („Gate-Flush“) einen Batch:

```json
{
  "feed_session_id": "b1f0...",
  "feed_variant": "for_you",
  "client_sent_at": "2026-11-12T10:00:00.000Z",
  "items": [
    { "post_id": 7, "position": 0, "shown_at": "2026-11-12T09:59:41.120Z",
      "dwell_ms": 4200, "voted": false, "opened_comments": true,
      "shared": false, "reported": false }
  ]
}
```

- `feed_session_id`: neu bei erstem Laden, Pull-to-Refresh und Wechsel Global/Lokal, **nicht**
  beim Nachladen.
- `feed_variant`: `"for_you"` oder `"local"`. Bewusst kein fester Wert in der Validierung: Ein
  neuer Feed-Typ in der App soll nicht jeden Batch mit 422 verwerfen.
- `position`: Platz im Feed beim **ersten** Sichtbarwerden. Gegen den Positions-Effekt: Platz 0
  wird immer mehr angeschaut als Platz 9, egal wie gut der Post ist.
- `dwell_ms`: Summe aller Sichtphasen. Unter 1000 schickt die App gar nicht, bei 180000 kappt
  sie. **Genau 180000 heißt „gekappt“, nicht „gemessen“.**
- Höchstens 500 Items pro Batch, nur als Schutz vor Missbrauch.

Antwort ist immer **204** ohne Inhalt.

### Die App schickt kumulativ

Derselbe Post kommt bei **jedem** Flush der Sitzung wieder, mit gewachsenem `dwell_ms`. Das ist
der Normalfall, kein Fehler. Deshalb ist das Speichern ein **Upsert** auf
`(user_id, feed_session_id, post_id)`, und zwar so, dass die **Reihenfolge der Ankunft egal ist**:

| Feld | Bei erneutem Eintreffen |
|---|---|
| `dwell_ms` | das **Maximum** von alt und neu |
| `voted`, `opened_comments`, `shared`, `reported` | **ODER**: einmal `true`, immer `true` |
| `position`, `shown_at` | bleiben vom ersten Mal |
| `features`, `feature_version` | bleiben vom ersten Mal |
| `received_at` | bleibt vom ersten Mal |

**Warum Maximum statt Überschreiben?** Der 10-s-Timer und ein Gate-Flush können gleichzeitig
unterwegs sein. Kommt der ältere, kleinere Wert zuletzt an, würde die Verweildauer sonst
schrumpfen.

**Warum die Features nie überschreiben?** Der erste Flush liegt am nächsten an dem Moment, in dem
der Post wirklich gezeigt wurde. Spätere Flushes sähen schon die Likes, die der Post *danach*
bekommen hat.

**Warum `user_id` im Schlüssel?** Die `feed_session_id` kommt vom Client. Ohne `user_id` könnte
jemand mit einer geratenen Session-ID die Zeilen eines anderen überschreiben. `user_id` kommt wie
immer aus dem Token.

### Lieber eine Zeile verlieren als den Batch

Die App versucht es **nicht erneut** und liest die Antwort nicht. Was hier mit einem Fehler
abbricht, ist für immer weg. Darum räumt der Endpoint vorher alles weg, was das eine
`INSERT ... ON CONFLICT` kippen würde:

1. **Doppelte `post_id` im selben Batch** werden vorher zusammengelegt, nach denselben Regeln wie
   oben. Postgres lehnt sonst das ganze Statement ab („cannot affect row a second time“).
2. **Gelöschte Posts** fliegen raus. `feed_impressions.post_id` hat einen Fremdschlüssel, eine
   einzige Zeile mit gelöschtem Post würde den ganzen Batch scheitern lassen.

### Drei Abfragen statt 3 × N

Für den Snapshot braucht jeder Post seinen Autor, „folge ich dem?“ und die Kommentarzahl. Das
kommt in **drei** Abfragen für den ganzen Batch (`_load_batch_context`): Posts mit Autor per JOIN,
Follows nur für die vorkommenden Autoren, Kommentarzahlen per `GROUP BY`. Danach nur noch
Nachschlagen im Dict. Posts ohne Kommentar fehlen im `GROUP BY`-Ergebnis, deshalb immer
`.get(post_id, 0)`.

### Warum Rohwerte und nicht der fertige Vektor

In `features` (JSONB) steht der **`FeatureInput`**: Alter in Stunden, Likes, Textlänge usw., noch
ungerechnet. Nicht die 17 skalierten Zahlen. So kann man `build_features` später ändern
(neue `FEATURE_VERSION`) und über die alten Daten neu laufen lassen. Ein gespeicherter Vektor wäre
mit der ersten Änderung wertlos.

## 2. Die Features

`build_features` macht aus einem `FeatureInput` genau 17 Zahlen, alle grob zwischen 0 und 1:

| # | Feature | Rechnung |
|---|---|---|
| 0 | Alter | `log1p(Stunden) / 7` (30 Tage ≈ 1) |
| 1 | Likes | `log1p(vote_count) / 7` |
| 2 | Kommentare | `log1p(Anzahl) / 5` |
| 3 | hat Bild | 0 / 1 |
| 4 | Titellänge | Zeichen / 100, gedeckelt bei 1 |
| 5 | Textlänge | Zeichen / 500, gedeckelt bei 1 |
| 6–9 | Flag | One-Hot: keins / engagement / creativity / productivity |
| 10 | ich folge dem Autor | 0 / 1 |
| 11 | gemeinsame Vibes | 0, 0,5 oder 1 (0, 1 oder 2 Treffer) |
| 12 | gleicher Ort | 0 / 1 |
| 13 | XP des Autors | `log1p(xp) / 10` |
| 14 | Streak des Autors | Tage / 30, gedeckelt bei 1 |
| 15–16 | Uhrzeit des Posts | `sin` und `cos` der Stunde |

**Warum `log1p`?** Ein Post mit 1000 Likes ist nicht 1000-mal besser als einer mit 1 Like. Rohe
Zähler lassen ein paar Ausreißer das ganze Training bestimmen.

**Warum alles auf 0..1?** Ungleich große Zahlen sind der häufigste Grund, warum ein Training
„einfach nicht lernt“.

**Warum Sinus und Kosinus für die Uhrzeit?** Als Zahl 0–23 lägen 23 Uhr und 0 Uhr maximal weit
auseinander, obwohl es eine Stunde ist. Auf dem Kreis liegen sie nebeneinander.

### Der Vertrag

- **Reihenfolge nie ändern**, nur hinten anhängen.
- Bei **jeder** Änderung an den Namen oder an der Rechnung `FEATURE_VERSION` erhöhen und neu
  trainieren. Ein altes Modell auf neuen Features rechnet ohne Fehlermeldung Unsinn.
- `features.py` und `ranker.py` nutzen **nur die Standardbibliothek**, keine Datenbank. So laufen
  sie im Railway-Container und im Training gleich. torch kommt nie nach Railway.
- Aus ORM-Objekten wird ein `FeatureInput` **nur** in `feature_input_from_orm`. Feed und
  Impressions benutzen beide diese eine Funktion. Würde jede Seite z. B. die Kommentare anders
  zählen, würde das Modell im Feed leise schlechter, und niemand fände den Grund
  („Training/Serving-Skew“).

## 3. Das Label: was ist ein guter Post?

`training/labels.py` macht aus dem Verhalten bei **einer** Impression einen Zielwert zwischen 0
und 1. Das ist eine Meinung, keine Mechanik, deshalb steht sie in einer eigenen Datei.

```
gemeldet                          → 0,0
geliked / Kommentare / geteilt    → 1,0
sonst                             → bis 0,6, je nach Verweildauer
```

Die Reihenfolge ist Absicht: Wer erst liked und dann meldet, bekommt 0.

**Gemeldet bleibt ein eigenes Signal.** Die vier Felder dürfen nie zu einem „hat reagiert“
zusammengefasst werden. Sonst lernt das Modell, gemeldete Posts häufiger zu zeigen.

### Verweildauer im Verhältnis zur Lesezeit

Nicht die rohe Zeit zählt, sondern **wie lange im Vergleich zu dem, was der Post hergibt**:

```
erwartete Lesezeit = 1,5 s + Zeichen / 20 + (2 s, wenn Bild)
Verhältnis         = Verweildauer / erwartete Lesezeit
Label              = 0,6 × min( log(1 + Verhältnis) / log(1 + 3), 1 )
```

Ab dreifacher Lesezeit ist die Skala voll. Beispiele:

| Post | erwartet | geschaut | Label |
|---|---|---|---|
| 200 Zeichen, kein Bild | 11,5 s | 1,5 s (vorbeigescrollt) | 0,05 |
| 200 Zeichen, kein Bild | 11,5 s | 12 s (gelesen) | 0,31 |
| 200 Zeichen, kein Bild | 11,5 s | 35 s (hängen geblieben) | 0,60 |
| 50 Zeichen + Bild | 6 s | 2 s | 0,13 |
| 50 Zeichen + Bild | 6 s | 18 s | 0,60 |

**Warum durch die Lesezeit teilen?** Mit roher Zeit würde das Netz die billigste Regel lernen:
„langer Text = gut“. Die Textlänge ist Feature 5, die Abkürzung läge direkt vor ihm.

**Warum höchstens 0,6?** Ein echtes Like soll immer mehr wiegen als langes Anschauen.

Die fünf Zahlen in `labels.py` sind geschätzt. Das ist in Ordnung, weil das Label erst beim
Training entsteht. Man kann sie drehen und neu trainieren, ohne eine einzige Impression neu zu
sammeln. Zum Ausprobieren:

```powershell
.\venv\Scripts\python.exe -m training.label_check
```

## 4. Training (lokal, nie auf Railway)

```powershell
.\venv\Scripts\python.exe -m training.simulate   # erfundene Daten → training/data/sim.jsonl
.\venv\Scripts\python.exe -m training.train      # → training/data/lytir.pt
.\venv\Scripts\python.exe -m training.export     # → training/data/lytir.json + lytir.onnx
```

`training/data/` steht in `.gitignore`. Ein Modell aus erfundenen Daten gehört nicht ins Repo.

- **Netz:** 17 → 32 → 16 → 1, mit ReLU dazwischen (`training/model.py`). Rund 1100 Parameter.
  Absichtlich winzig: Bei 17 Features würde ein großes Netz die Beispiele auswendig lernen.
- **Ausgabe ist ein Logit**, kein Sigmoid. Fürs Sortieren reicht der Logit, Sigmoid ändert die
  Reihenfolge nicht. Im Training geht er direkt in `BCEWithLogitsLoss`, das auch Zielwerte wie
  0,31 versteht.
- **Aufteilung nach Feed-Sitzung und Zeit**, nicht zufällig pro Zeile: älteste 80 % der
  Sitzungen zum Lernen, neueste 20 % zum Prüfen. Zwei Zeilen derselben Sitzung sind keine
  unabhängigen Beispiele. Lägen sie auf beiden Seiten, sähe die Prüfung toll aus und hieße nichts.
- **Gespeichert wird die Epoche mit der besten Prüf-Loss**, nicht die letzte.
- Aus der Datenbank (`load_rows`) kommen nur Zeilen mit aktueller `FEATURE_VERSION` und **ohne
  eigene Posts**. Am eigenen Post scrollt man anders vorbei. Global und Lokal gehen in **ein**
  Modell, getrennt würden die ohnehin knappen Daten halbiert.

**Export:** `export.py` schreibt das Netz zweimal, als ONNX und als nacktes JSON (Gewichte pro
Schicht plus `feature_version`). Danach rechnet es beide Dateien gegen PyTorch nach und bricht ab,
wenn eine um mehr als 0,00001 abweicht. Der JSON-Check benutzt dafür genau `_logit` aus
`ranker.py`.

## 5. Der Ranker im Feed

`ranker.py` rechnet das Netz aus `app/lytir/lytir.json` mit reinem Python nach: Pro Schicht
Gewichte mal Eingabe plus Bias, ReLU dazwischen, nicht nach der letzten. Gemessen ~10 ms für 150
Kandidaten.

- **Die Architektur steht im Code, nicht in der Datei.** Ändert sich `training/model.py`, muss
  `_logit` mit.
- **Keine Datei → kein Modell.** `scores()` gibt `None`, der Feed gibt jedem Post 0 und behält die
  SQL-Reihenfolge.
- **Falsche `feature_version` → kein Modell**, mit einer Warnung im Log.
- Die Datei wird **einmal pro Worker** geladen (`lru_cache`). Eine neue Datei wirkt erst nach
  einem Neustart, also nach dem Deploy.
- **Ein gemeinsamer Zeitpunkt** für alle 150 Kandidaten. Sonst wäre Post 150 ein paar
  Mikrosekunden „älter“ als Post 1, nur weil er später berechnet wurde.

**Warum JSON statt ONNX im Backend?** `onnxruntime` wäre eine schwere Abhängigkeit im Container
für eine Rechnung, die in 30 Zeilen Python passt. Die ONNX-Datei entsteht trotzdem mit, falls das
Ranking später auf das Handy wandert.

## 6. Live schalten

Erst wenn es genug echte Impressions gibt:

1. `training/train.py` auf echte Daten umstellen. Heute liest es fest
   `training/data/sim.jsonl`. Für echte Daten `load_rows(db)` aus `dataset.py` benutzen.
2. Trainieren und exportieren. Die Prüf-Loss muss **deutlich** unter der Messlatte liegen, die
   `train.py` ausgibt (ein „Modell“, das immer den Durchschnitt tippt).
3. `training/data/lytir.json` nach `app/lytir/lytir.json` kopieren.
4. `scripts/rerank_vergleich.py` V10 laufen lassen (V1–V9 dafür die Datei kurz wegschieben).
5. `LYTIR_RERANK_ENABLED = True` in `ranking_config.py`. Bewusst eine Konstante im Code und
   keine Umgebungsvariable: Im Git soll sichtbar sein, wann und mit welchem Modell umgelegt wurde.
6. Beides zusammen committen und deployen.

Ab dann gilt für den Feed der Rerank-Weg mit Reihenfolge-Cache, siehe [feed.md](feed.md),
Abschnitt 6.

**Offene Überlegung:** Das Ranking später aufs Handy verlegen, um Serverkosten zu sparen. Ein
Dart-Nachbau von `build_features` und `_logit` ist machbar. Der Preis: Das Handy bräuchte die
Rohwerte (XP, Streak, Vibes des Autors), müsste die Zeitzone exakt wie der Server behandeln, und
zwei Implementierungen derselben Rechnung können auseinanderlaufen.

## Bekannte Lücken

- **`POST /impressions/` hat kein Rate-Limit** und prüft nicht, ob der Post mir überhaupt
  angezeigt werden konnte (Block, gemeldet). Ein eigener Client könnte Trainingsdaten verfälschen.
- **`feed_impressions` wächst unbegrenzt.** Es gibt keinen Cleanup und keine Aufbewahrungsfrist.
- **`feed_variant` wird nicht geloggt**, obwohl der Kommentar in `schemas.py` das sagt.
- **Veralteter Docstring:** `app/lytir/__init__.py` spricht noch von `lytir.onnx`, der Ranker
  liest aber `lytir.json`.
- **`created_hour` hängt an der Zeitzone der Datenbank-Verbindung.** Supabase liefert UTC, dann
  ist die Stunde UTC. Ändert sich das, verschieben sich Feature 15–16 still.

## Vor dem Launch

- Datenschutzerklärung: Speicherung der Impressions (was gesehen, wie lange, Reaktion) und Zweck
  (Feed-Sortierung) nennen, dazu eine Aufbewahrungsfrist festlegen.
- Der geplante Datenexport muss `feed_impressions` umfassen.

## Was der Test prüft

- `scripts/rerank_vergleich.py` **V10**: Mit den Gewichten aus `training/data/lytir.json` (nur
  in diesem Prozess) kommen dieselben Posts wie ohne Modell, aber nach Score sortiert, weiterhin in
  einer Anweisung. Dazu eine Zeitmessung für 150 Kandidaten. V1–V9 stehen in
  [feed.md](feed.md#was-der-test-prüft).
- `training/export.py` prüft beim Export selbst, dass ONNX und JSON dasselbe rechnen wie PyTorch.
- `training/label_check.py` ist kein Test, sondern eine Tabelle zum Draufschauen, wenn man an den
  Label-Zahlen dreht.
- Für das Upsert in `POST /impressions/` gibt es kein eigenes Testskript. Das SQL steht im
  SQL-Snapshot (`_impression_upsert`).
- `scripts/personalization_toggle_test.py` **Block C** prüft die Einwilligung: Mit Schalter an
  wird geschrieben. Ein `PUT` ohne das Feld oder ein erneutes `true` löscht nicht. Ein Widerruf
  löscht nur die eigenen Zeilen, andere User behalten ihre. Danach kommt 204 und es wird nichts
  geschrieben.
