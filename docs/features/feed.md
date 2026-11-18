# Feed

**Code:** `app/feed.py` (die ganze Logik, Einstieg `seite_holen`), `app/routers/post.py`
(`GET /posts/`, nur HTTP), Gewichte in `app/ranking_config.py`, Likes in `app/routers/vote.py`,
Orte in `app/routers/location.py`
**Tests:** `scripts/rerank_vergleich.py` (V1–V10), `scripts/feed_query_test.py` (Messung),
`scripts/personalization_toggle_test.py` (Personalisierung aus)
**Verwandt:** [lytir.md](lytir.md), [tagesranking.md](tagesranking.md)

## Worum es geht

Der Feed ist die Startseite der App: eine endlose Liste von Posts, seitenweise nachgeladen.
Es gibt zwei Arten:

- **Global** („Für dich“): alle Posts.
- **Lokal:** nur Posts aus meinem Ort.

Dazu kommt die **Suche** nach Post-Titeln.

Die Reihenfolge entsteht heute aus einer **festen Formel in SQL** (Score aus Likes, Alter,
Folgen, Vibes). Später soll das Modell **Lytir** die Posts neu sortieren. Der Code dafür ist
fertig, aber ausgeschaltet (`LYTIR_RERANK_ENABLED = False`), weil es noch kein Modell aus echten
Daten gibt. Alles zu Lytir steht in [lytir.md](lytir.md). Diese Seite beschreibt beide Wege, damit
klar ist, was sich beim Umschalten ändert.

Jeder User kann die Personalisierung **abschalten** (`personalized_feed = false`). Dann fallen
Folgen, Vibes und Lytir aus der Sortierung heraus (Abschnitt 7).

## Die Regeln auf einen Blick

1. Im normalen Feed sind nur Posts der **letzten 30 Tage**. Die Suche findet auch ältere.
2. Posts, die **ich gemeldet** habe, sehe ich nie wieder, auch nicht in der Suche.
3. Eigene Posts erscheinen ganz normal im Feed (`is_mine = true`).
4. Der **Lokal-Feed** braucht einen Ort am eigenen Profil, sonst **400**.
5. **Eine Feed-Seite kostet eine SQL-Anweisung**, egal welcher Weg. Alles, was die App pro Post
   wissen muss, kommt als Spalte mit.
6. Der Server rechnet die Reihenfolge. Die App schickt nur `limit`, `skip`, `search`, `local`.
7. **Personalisierung aus** heißt: kein Follow-Bonus, kein Vibe-Bonus, kein Lytir. Filter aus
   Meldungen und Blocks gelten weiter.

## Anfrage und Antwort (`GET /posts/`)

| Parameter | Standard | Bedeutung |
|---|---|---|
| `limit` | 10 | Posts pro Seite |
| `skip` | 0 | wie viele schon geladen sind (Seite 2 = `skip=10`) |
| `search` | `""` | Teil des Titels; leer = normaler Feed |
| `local` | `false` | nur Posts aus meinem Ort |

| Status | Wann |
|---|---|
| 400 `user has no location set` | `local=true`, aber ich habe keinen Ort gesetzt |
| **200** | Liste von Einträgen, siehe unten (kann kürzer als `limit` sein) |

Jeder Eintrag:

```json
{
  "post": { "id": 7, "title": "...", "owner": { ... }, "location": { ... }, ... },
  "votes": 12,
  "is_mine": false,
  "is_liked": true
}
```

- `votes` ist der Like-Zähler `posts.vote_count` (siehe [unten](#der-like-zähler-vote_count)).
- `is_liked`: habe ich diesen Post geliked.

Eine **kürzere Seite als `limit`** heißt nicht immer „Ende des Feeds“. Im Rerank-Weg fallen
Posts heraus, die seit dem Merken der Reihenfolge gelöscht oder gemeldet wurden (Abschnitt 6).
Wirklich zu Ende ist der Feed erst bei einer **leeren** Seite.

## 1. Welche Posts kommen in Frage

Alle Filter sitzen in `_kandidaten_holen`:

| Filter | Gilt für | Warum |
|---|---|---|
| Titel enthält `search` | immer (bei `""` trifft alles) | Suche |
| nicht von mir gemeldet | immer | Meldungen „Stufe B“: Gemeldetes verschwindet für den Meldenden sofort, noch bevor ein Moderator draufschaut |
| kein Block mit dem Autor | immer, beide Richtungen | [README](../README.md), Regel 6 |
| `location_id` = mein Ort | nur `local=true` | Lokal-Feed |
| `created_at` ≥ jetzt − 30 Tage | nur ohne Suche | Datenbank muss den Score nur für junge Posts ausrechnen |

**Warum 30 Tage (`FEED_MAX_AGE_DAYS`)?** Pro Stunde Alter verliert ein Post 1 Punkt (Abschnitt
2). Nach 30 Tagen steht er bei −720 und käme ohnehin nie nach oben. Der Filter ändert das Ergebnis
also praktisch nicht, spart der Datenbank aber das Rechnen über die ganze Tabelle. `created_at`
hat einen Index.

**Warum gilt er bei der Suche nicht?** Wer gezielt einen Titel sucht, soll auch alte Posts finden.

**Meldung und Block als Unterabfrage:** „Habe ich das gemeldet?“ (`_ich_habe_gemeldet`) und
„Gibt es einen Block mit dem Autor?“ (`_block_besteht`) laufen als `NOT EXISTS` in derselben
Anweisung mit. Früher wurde erst die Liste der gemeldeten IDs geholt und dann gefiltert. Das war
ein Rundweg mehr (~14 ms) und schickte die IDs zweimal durchs Netz. Beide Filter stehen auch im
Cache-Weg (`_posts_nach_ids`): Wer jemanden blockiert, sieht dessen Posts ab der nächsten Seite
nicht mehr, auch wenn sie schon in der gemerkten Reihenfolge stehen.

## 2. Die Reihenfolge: der Score

Ohne Lytir sortiert die Datenbank nach diesem Score, höchster zuerst:

```
score =  1  × Likes
      −  1  × Alter in Stunden
      + 50  wenn ich dem Autor folge
      + 20  wenn der Autor und ich mindestens einen Vibe-Faktor teilen
```

Die Zahlen stehen in `ranking_config.py` (`FEED_VOTE_WEIGHT`, `FEED_AGE_PENALTY_PER_HOUR`,
`FEED_FOLLOW_BONUS`, `FEED_VIBE_BONUS`). Bei Gleichstand kommt der neuere Post zuerst.

**So kann man die Gewichte lesen:** Alles ist in „Stunden“ umrechenbar. Ein Like gleicht eine
Stunde Alter aus. Folgen ist so viel wert wie 50 Stunden, also gut zwei Tage Vorsprung. Ein
gemeinsamer Vibe ist 20 Stunden wert.

Beispiel, drei Posts im selben Moment:

| Post | Alter | Likes | folge ich | gleicher Vibe | Score |
|---|---|---|---|---|---|
| A | 2 h | 3 | ja | nein | 3 − 2 + 50 = **51** |
| B | 30 min | 10 | nein | ja | 10 − 0,5 + 20 = **29,5** |
| C | 1 Tag | 40 | nein | nein | 40 − 24 = **16** |

Reihenfolge: A, B, C. Post C bräuchte 36 Likes mehr, um A einzuholen.

**Vibes:** Jeder User hat zwei Vibe-Faktoren (`vibe_factor_1`, `vibe_factor_2`). Der Bonus kommt,
sobald einer meiner beiden bei einem der beiden des Autors vorkommt. Zwei Treffer geben **nicht**
mehr als einer. Hat jemand keine Vibes gesetzt, gibt es für ihn nie einen Bonus.

**Ohne Personalisierung** (Abschnitt 7) bleiben nur die ersten beiden Zeilen:

```
score = 1 × Likes − 1 × Alter in Stunden
```

Dieselben drei Posts sortieren sich dann ganz anders:

| Post | Score ohne Personalisierung |
|---|---|
| C | 40 − 24 = **16** |
| B | 10 − 0,5 = **9,5** |
| A | 3 − 2 = **1** |

Reihenfolge: C, B, A. Es zählt nur noch, was alle anderen auch sehen: Likes und Alter.

**Das Alter rechnet die Datenbank** mit `now()`. Zwischen zwei Seiten vergeht Zeit, aber alle
Posts altern gleich schnell. Die Reihenfolge verschiebt sich dadurch nicht, nur durch neue Posts
und neue Likes.

## 3. Alles in einer Anweisung

Neben dem Post braucht die App bzw. Lytir pro Post noch drei Dinge. Sie kommen als **zusätzliche
Spalten** in derselben SQL-Anweisung (`_zusatz_spalten`), nicht als eigene Abfragen:

| Spalte | SQL | Wofür |
|---|---|---|
| `is_liked` | `EXISTS` auf `votes` | Herz in der App |
| `i_follow_owner` | `EXISTS` auf `follows` | Follow-Bonus und Lytir-Feature (kommt auch ohne Personalisierung mit, wird dann nur nicht für den Score benutzt) |
| `comment_count` | `count(*)` als Unterabfrage auf `comments` | Lytir-Feature |

Autor und Ort hängen per `joinedload` als JOIN mit dran.

`i_follow_owner` und `comment_count` gibt `GET /posts/` **nicht** an die App weiter. Sie kommen
trotzdem mit, damit Lytir später keinen zweiten Rundweg braucht.

**Was das gebracht hat** (Umbau 2026-09-20): Vorher waren es 4 Anweisungen pro Seite, in
Produktion mit Lazy-Loads der Autoren sogar 17. Jetzt ist es 1. Weil jede Anweisung grob 15 ms
Rundweg kostet ([README](../README.md), Regel 2), ist das der größte Hebel. Gemessen wurde auch:
`limit=10` kostet 1,1 Rundwege, `limit=150` nur 1,3. Mehr Zeilen sind fast gratis, deshalb kann
der Rerank-Weg (Abschnitt 6) 150 Kandidaten auf einmal holen.

## 4. Lokal-Feed und Orte

Orte sind ein **fester Katalog** (`locations`), keine freie Texteingabe. So landen „München“ und
„Muenchen“ nicht in zwei verschiedenen Lokal-Feeds.

- **Ort am Profil:** `users.location_id`. Ohne ihn liefert `local=true` 400.
- **Ort am Post:** `posts.location_id`. Schickt die App beim Posten keinen mit, bekommt der Post
  den Ort des Users **zum Zeitpunkt des Postens**. Zieht der User später um, bleiben seine alten
  Posts am alten Ort. Ein mitgeschickter Ort, den es nicht gibt, gibt **404**.
- **Ort gelöscht:** Der Fremdschlüssel steht auf `SET NULL`. Der Post bleibt, fällt aber aus
  jedem Lokal-Feed heraus.

**Orte suchen** (`GET /locations/?search=Mün`): höchstens 20 Treffer, Groß-/Kleinschreibung egal.
Orte, die mit dem Suchtext **anfangen**, stehen vor denen, die ihn nur enthalten, danach
alphabetisch.

**Orte anlegen** (`POST /locations/`): nur mit Header `X-Location-Admin-Secret`, kein Login-Token.
Body `{"names": ["Berlin", "Hamburg", ...]}`. Schon vorhandene Namen werden übersprungen. Antwort:
`{"created": 2, "skipped": 1}`. Das ist das Werkzeug zum Befüllen des Katalogs, nicht für die App.

## 5. Suche

`search` filtert auf den **Titel** (`title LIKE '%suchtext%'`), nicht auf den Inhalt. Dabei:

- Groß-/Kleinschreibung **zählt** (`LIKE`, nicht `ILIKE`). „berlin“ findet „Berlin“ nicht.
- keine 30-Tage-Grenze,
- sortiert nach demselben Score wie der Feed,
- geht **immer** den alten SQL-Weg, auch wenn Lytir an ist, und benutzt keinen Cache.

## 6. Die zwei Wege und der Reihenfolge-Cache

`seite_holen` entscheidet:

```
         Lytir an UND personalisiert UND keine Suche?
                        │                    │
                       nein                  ja
                        │                    │
                 Alter Weg              Rerank-Weg
     SQL sortiert, SQL schneidet    150 holen, Lytir sortiert,
     mit LIMIT/OFFSET die Seite     Reihenfolge in Redis merken,
     aus. Kein Cache.               Python schneidet die Seite aus.
     Score mit oder ohne Bonus,
     je nach personalized_feed.
```

Heute ist der Schalter aus, es läuft also **nur der alte Weg**. Der Rest dieses Abschnitts gilt
erst, wenn [Lytir](lytir.md) live geht.

### Warum ein Cache?

Mit LIMIT/OFFSET sortiert die Datenbank bei jeder Seite neu. Das geht, weil ihre Sortierung
stabil ist. Lytir sortiert aber in Python, und zwar immer nur einen Block von Kandidaten. Würde
man für Seite 2 einfach neu rechnen, käme eine andere Reihenfolge heraus, und der Nutzer sähe
Posts doppelt oder gar nicht. Deshalb wird die **fertige Reihenfolge** gemerkt: eine Liste von
Post-IDs in Redis.

| | |
|---|---|
| Key | `feed_order:{user_id}:global` bzw. `:local` (Global und Lokal haben verschiedene Reihenfolgen) |
| Wert | JSON-Liste der Post-IDs, z. B. `[47, 12, 93, ...]` |
| Lebensdauer | **300 s** (`FEED_ORDER_CACHE_TTL`) ab dem letzten Schreiben |

### Ablauf an einem Beispiel (`limit=10`)

```
Seite 1, Cache leer
  → SQL: 150 Kandidaten nach Score          (1 Anweisung)
  → Lytir sortiert die 150 um
  → alle 150 IDs in Redis, TTL 300 s
  → Posts 1–10 zurück (schon geladen, keine weitere Anweisung)

Seite 2 (skip=10), 40 s später
  → Cache hat 150 IDs, braucht 20 → reicht
  → IDs 11–20 aus der Liste
  → SQL: WHERE id IN (...)                  (1 Anweisung)
  → in der Reihenfolge der Liste zurück, TTL wird NICHT verlängert

Seite 16 (skip=150)
  → Cache hat 150, braucht 160 → nachladen
  → SQL: nächste 150 Kandidaten mit OFFSET 150, Lytir sortiert diesen Block
  → IDs, die schon in der Liste stehen, fallen raus
  → Liste (jetzt bis zu 300) neu in Redis, TTL wieder 300 s
```

Wichtige Folge: **Lytir sortiert immer nur innerhalb eines Blocks von 150.** Welche 150 Posts
überhaupt in den ersten Block kommen, entscheidet weiterhin der SQL-Score. Ein Post auf SQL-Platz
200 kann nicht auf Platz 1 springen.

### Die Details, die man leicht kaputt macht

- **TTL nur bei echtem Zuwachs verlängern.** Würde jede Anfrage den Eintrag neu schreiben,
  bliebe die Reihenfolge für einen fleißig scrollenden Nutzer ewig eingefroren, und neue Posts
  kämen nie.
- **Doppelte rauswerfen** beim Nachladen. Zwischen zwei Blöcken kann ein neuer Post oder ein
  neues Like die SQL-Sortierung verschieben. Dann liefert OFFSET 150 einen Post, der schon im
  ersten Block war.
- **Abbruch, wenn ein Block nichts Neues bringt.** Sonst würde die Schleife endlos dieselben
  Posts holen. Das kann den Feed in seltenen Fällen etwas zu früh beenden. Bewusst so entschieden
  (2026-09-20).
- **Abbruch bei einem kurzen Block** (weniger als 150). Dann hat die Datenbank nichts mehr, und
  eine weitere Anweisung würde nur „leer“ zurückmelden.
- **IDs ohne Post fallen still heraus.** Zwischen Merken und Abrufen kann ein Post gelöscht, von
  mir gemeldet oder sein Autor blockiert worden sein. Die Seite ist dann kürzer.
- **Redis kaputt = Cache leer.** Lesen und Schreiben schlucken Redis-Fehler. Der Feed rechnet
  dann jede Seite neu. Er läuft weiter, kann aber Posts doppelt zeigen.
- **Ohne Modell ist der Rerank-Weg identisch zum alten.** Fehlt `app/lytir/lytir.json`, bekommt
  jeder Post den Score 0. Python sortiert stabil, also bleibt die SQL-Reihenfolge. Genau das prüft
  `rerank_vergleich.py` (V1).

## 7. Personalisierung abschalten (`personalized_feed`)

Jeder User hat den Schalter `users.personalized_feed`, Standard **an**. Wer ihn ausschaltet,
bekommt einen Feed, der nicht aus seinem Verhalten oder Profil vorhersagt, was ihm gefallen
könnte.

**Warum es den Schalter gibt:** Follow-Bonus, Vibe-Bonus und Lytir sind Profiling im Sinne von
Art. 4 Nr. 4 DSGVO. Aus dem, wem ich folge und welche Vibes ich habe, wird vorhergesagt, was mich
interessiert. Dagegen hat jeder ein Widerspruchsrecht (Art. 21). Art. 27 DSA verlangt außerdem,
dass wir die Hauptkriterien der Sortierung offenlegen und, wo es Optionen gibt, sie bedienbar
machen. Deshalb ist es **Opt-out**: an, bis jemand widerspricht.

### Was sich ändert und was nicht

| | personalisiert | aus |
|---|---|---|
| Follow-Bonus (+50) | ✓ | – |
| Vibe-Bonus (+20) | ✓ | – |
| Lytir-Rerank (wenn eingeschaltet) | ✓ | – |
| Likes und Alter im Score | ✓ | ✓ |
| Filter: gemeldet, Block, 30 Tage, Lokal, Suche | ✓ | ✓ |
| `is_liked`, `i_follow_owner`, `comment_count` als Spalten | ✓ | ✓ |
| Eine SQL-Anweisung pro Seite | ✓ | ✓ |

**Warum Meldungen und Blocks bleiben:** Das ist kein Profiling. Der Server sagt dabei nichts
voraus, er führt eine ausdrückliche Anweisung des Users aus („diesen Post / diese Person will ich
nicht sehen“). Rechtsgrundlage ist der Vertrag (Art. 6 Abs. 1 lit. b), und Art. 21 gilt nur für
lit. e und f. Der Follow-**Bonus** ist dagegen eine Vorhersage („wem du folgst, den willst du
weiter oben sehen“) und fällt deshalb weg.

**Der Lokal-Feed bleibt:** Den wählt der User für jede Anfrage selbst (`local=true`). Das ist
ein Filter, den er bewusst setzt, keine Vorhersage.

### Wie es im Code umgesetzt ist

Es gibt **keinen dritten Code-Weg**, sondern einen Parameter: `seite_holen` liest
`current_user.personalized_feed` und gibt ihn als `personalisiert` an `_kandidaten_holen`
weiter. Dort entscheidet er nur, ob Follow- und Vibe-Bonus in den Score kommen. Alles andere
(Filter, Spalten, Sortierung, LIMIT/OFFSET) ist für beide Modi derselbe Code. Bewusst so: Eine
Kopie der Funktion hätte jeden späteren Filter doppelt gebraucht. Wird er in einer Kopie
vergessen, sehen genau die Leute, die widersprochen haben, plötzlich Posts von Blockierten.

Der Rerank-Weg ruft `_kandidaten_holen` mit `personalisiert=True` auf. Er wird nur erreicht, wenn
`seite_holen` das vorher geprüft hat.

### Setzen und lesen

- **Setzen:** `PUT /users/` mit `{"personalized_feed": false}` (wie `ranking_enabled`).
  Explizites `null` wird ignoriert und ändert nichts. Die Spalte ist `NOT NULL`, ohne diesen Filter
  käme sonst 500.
- **Lesen:** `GET /users/` (eigenes Profil) liefert `personalized_feed`. Bei fremden Profilen
  und in der Suche ist das Feld `null`. Ob jemand personalisiert schaut, geht andere nichts an.
- **Wirkt sofort:** `get_current_user` lädt die User-Zeile bei jeder Anfrage. Die nächste
  Feed-Seite kommt schon ohne Bonus. Bewusst **kein** Redis-Cache für den Schalter: Mit einer TTL
  würde nach dem Widerspruch noch eine Weile personalisiert.

### Gilt auch fürs Tagesranking

Derselbe Schalter nimmt im [Tagesranking](tagesranking.md) den Vibe- und den Orts-Bonus aus dem
Gewicht des Targets. Die App sollte ihn deshalb nicht „Feed“ nennen, sondern etwa
**„Personalisierte Inhalte“**.

### Abgrenzung zu den Impressions

Die Impressions (was ich gesehen habe, wie lange) hängen an einem **eigenen** Schalter
`impressions_enabled`, siehe [lytir.md](lytir.md#nur-mit-einwilligung). Getrennt, weil es zwei
verschiedene Zwecke mit verschiedenen Rechtsgrundlagen sind: Sortieren ist Opt-out (Widerspruch,
Art. 21), Trainingsdaten sammeln ist Opt-in (Einwilligung, Art. 6 Abs. 1 lit. a). Mit einer
einzigen Spalte ginge das nicht, weil die Standardwerte verschieden sind.

## Der Like-Zähler (`vote_count`)

`posts.vote_count` ist ein mitgeschriebener Zähler ([README](../README.md), Regel 4). Er soll immer
gleich der Anzahl der Zeilen in `votes` für diesen Post sein. Die Tabelle `votes` ist die Quelle
der Wahrheit, der Zähler nur die schnelle Kopie für den Feed.

| Ereignis | Was passiert | Wo |
|---|---|---|
| Like (`POST /vote/`, `dir: 1`) | Zeile anlegen, Zähler +1 im selben Commit; schon geliked → 409 | `vote.py` |
| Like zurück (`dir: 0`) | Zeile löschen, Zähler −1 im selben Commit; nicht geliked → 404 | `vote.py` |
| Liker löscht sein Konto | **vorher** −1 auf jedem Post, den er geliked hat; danach räumt CASCADE die Votes weg | `_delete_user_data` in `user.py`, Schritt 3 |
| Besitzer löscht den Post | Post und Votes weg (CASCADE) | |
| Moderator stellt Post wieder her | Votes zurück, Zähler **neu gezählt** | `admin.py`, `_restore_post` |

Das +1 ist ein `UPDATE ... SET vote_count = vote_count + 1` in SQL, nicht „lesen, in Python
addieren, schreiben“. Zwei gleichzeitige Likes würden sich sonst gegenseitig überschreiben.

## Bekannte Lücken

- **Der Feed beachtet keine Sperren.** Posts gesperrter Nutzer erscheinen normal. Ob das gewollt
  ist (wie im Profil), ist nicht entschieden.
- **`limit` hat keine Obergrenze.** `limit=100000` liefert alle Posts der letzten 30 Tage auf
  einmal. Im Rerank-Weg lädt die Schleife dann so viele Blöcke, wie nötig sind. Ein
  `Query(le=50)` wie beim Profil-Ranking würde reichen.
- **Doppelte Posts im alten Weg.** Kommt zwischen Seite 1 und 2 ein neuer Post mit hohem Score
  dazu, rutscht alles eine Stelle nach hinten, und OFFSET liefert den letzten Post von Seite 1
  noch einmal. Die App sollte nach Post-ID entdoppeln.
- **Pull-to-Refresh im Rerank-Weg.** Innerhalb der 300 s Cache bekommt man nach einem Refresh
  wieder dieselbe Reihenfolge. Es gibt keinen Parameter, um den Cache zu verwerfen. Läuft er
  umgekehrt mitten im Scrollen ab, wird ab der nächsten Seite neu sortiert und es kann zu
  Dopplungen kommen.
- **Suche:** nur Titel, Groß-/Kleinschreibung zählt, `%` und `_` im Suchtext wirken als
  Platzhalter.
- **Einzelner Post (`GET /posts/{id}`)** prüft weder Blocks noch Meldungen.
- **Der SQL-Snapshot deckt den Feed nicht mehr ab.** `tests/test_sql_snapshot.py` baut die alte
  Feed-Abfrage selbst nach. Besser wäre, dort `feed._kandidaten_holen(...)` aufzurufen und nur das
  SQL zu kompilieren, dann fällt jede Änderung an `feed.py` im Diff auf.

## Was der Test prüft

`scripts/rerank_vergleich.py` (liest nur, schreibt nur eigene Redis-Keys und löscht sie wieder).
V1–V9 brauchen den Zustand **ohne** Modell (`app/lytir/lytir.json` darf nicht existieren).
Den Schalter legt das Skript nur in seinem eigenen Prozess um.

| | Prüft |
|---|---|
| V1 | Rerank-Weg liefert dieselben Seiten wie der alte Weg (IDs, votes, is_mine, is_liked) |
| V2 | Rerank-Weg braucht genau 1 SQL-Anweisung |
| V3 | nach Seite 1 steht die komplette Reihenfolge mit TTL in Redis |
| V4 | Suche umgeht Rerank und Cache |
| V5 | Lokal-Feed |
| V6 | Seite 2 aus dem Cache, 1 Anweisung, TTL wird nicht verlängert |
| V7 | mit Blockgröße 5 wird korrekt nachgeladen |
| V8 | eine ID ohne Post im Cache fällt heraus |
| V9 | Redis kaputt: richtige Seiten trotzdem |
| V10 | echtes (simuliertes) Modell: sortiert nach Score, 1 Anweisung, Zeitmessung |

`scripts/personalization_toggle_test.py` (gegen die echte DB, wird zurückgerollt, kein Redis)
prüft den Schalter aus Abschnitt 7. Die Feed-Posts bekommen einen zufälligen Tag im Titel und
werden über `search` gesucht, damit echte Posts den Vergleich nicht stören.

| | Prüft |
|---|---|
| A | Standardwerte (`personalized_feed` an, `impressions_enabled` aus), fremde Profile zeigen beides nicht |
| B | Setzen per `PUT /users/`, explizites `null` ändert nichts und gibt keinen 500 |
| C | Impressions: siehe [lytir.md](lytir.md#was-der-test-prüft) |
| D | gleiche Posts, personalisiert: Gefolgter mit gleichem Vibe vorn; aus: mehr Likes vorn; Block-Filter in beiden Modi; `i_follow_owner` bleibt |
| E | `seite_holen` nimmt den Rerank-Weg nur bei Lytir an **und** personalisiert **und** ohne Suche |
| F | Tagesranking-Gewicht, siehe [tagesranking.md](tagesranking.md) |

`scripts/feed_query_test.py` misst, wie viele Anweisungen ein Feed-Abruf braucht und wie sich die
Zeit auf Datenbank und Python verteilt. Zeigt sich dort eine Anweisung 150-mal, ist ein N+1
zurück.

`tests/test_sql_snapshot.py` enthält zwar eine Feed-Abfrage, aber eine **nachgebaute Kopie vom
Stand vor dem Umbau** (mit `notin_`-Liste statt `NOT EXISTS`, ohne die drei Zusatzspalten). Er
ruft `app/feed.py` nicht auf und merkt Änderungen dort deshalb nicht. Siehe Bekannte Lücken.
