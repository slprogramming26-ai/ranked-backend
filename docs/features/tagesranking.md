# Tagesranking

**Code:** `app/routers/ranking.py`, Werte in `app/ranking_config.py`
**Tests:** `scripts/daily_target_test.py`
**Verwandt:** [xp-streak-ligen.md](xp-streak-ligen.md), [profil-ranking.md](profil-ranking.md)

## Worum es geht

Einmal am Tag bekommt jeder Teilnehmer **eine fremde Person** zugeteilt, das „Target“. Er sieht
deren Posts der letzten 7 Tage und swipet sie: rechts = cool, links = nicht cool. Jeder Swipe
bringt dem Target Punkte. Wer am Tag die meisten Punkte bekommt, steht oben im **Leaderboard**.

Das Tagesranking ist zum **Entdecken** da: Man soll Leute sehen, denen man noch nicht folgt.
Deshalb sind Gefolgte ausgeschlossen (außer als Notlösung, siehe unten).

Teilnehmen heißt: `users.ranking_enabled = true`. Wer das nicht hat, wird nicht gezogen und
darf auch selbst nicht bewerten (`GET /ranking/my_target` → 403). Eingeschaltet wird es über
`PUT /users/` mit `{"ranking_enabled": true}`.

## Der Ablauf eines Tages

```
App                                  Backend
 │  GET /ranking/my_target  ───────▶  Target für heute ziehen (oder das schon gezogene liefern)
 │  ◀──── Target + bis zu 20 Posts
 │
 │  (Nutzer swipet in der App, die Swipes sammeln sich nur in der App)
 │
 │  POST /ranking/swipe_session ───▶  Punkte berechnen, speichern, XP + Streak buchen,
 │  ◀──── Punkte, XP, Streak            Punkte ins Live-Leaderboard (Redis)
 │
 │  GET /ranking/leaderboard  ─────▶  Top 7 + eigene Zeile
 │
 ⋮  um 00:05 UTC: pg_cron ruft POST /ranking/end_of_day → Bonus-XP für die Top 10 von gestern
```

Wichtig: Die Swipes werden **erst am Ende als ein Paket** geschickt (`swipe_session`). Bricht
jemand mittendrin ab, ist nichts gespeichert und er kann später neu anfangen.

## 1. Wer wird gezogen? (`GET /ranking/my_target`)

### Schon gezogen? Dann dasselbe noch mal

Jede Ziehung wird als Zeile in `daily_targets` gespeichert (`voter_id`, `target_user_id`,
`date`). Ruft die App `my_target` heute ein zweites Mal auf, kommt **dasselbe Target** zurück.
Man kann also nicht durch Neuladen ein anderes, „besseres“ Target würfeln.

### Harte Filter: Wer kommt überhaupt infrage?

Ein Kandidat muss **alle** diese Bedingungen erfüllen:

| Regel | Warum |
|---|---|
| nicht ich selbst | |
| `ranking_enabled = true` | Nur wer mitmacht, wird bewertet |
| mindestens ein Post in den letzten 7 Tagen | Sonst gäbe es nichts zu swipen |
| nicht gesperrt (Sperre läuft gerade) | Gesperrte sollen nicht gezeigt werden |
| kein Block, egal in welche Richtung | Siehe Regel 6 in der [README](../README.md) |
| **ich habe ihn nie gemeldet** | siehe unten |
| ich folge ihm nicht | Entdecken statt Bekannte; Ausnahme: Notlösung |

**Meldungen:** Hat man jemanden **irgendwann einmal** gemeldet, wird er einem **nie** wieder
vorgeschlagen. Egal was gemeldet wurde (Post, Story, Kommentar, Profil) und egal, wie die
Moderation entschieden hat, auch bei „abgelehnt“. Das ist ein harter Filter, kein
Gewichtsabzug. Grund: Wer jemanden gemeldet hat, will ihn nicht als Tagesaufgabe sehen.

### Notlösung: Gefolgte

Bleibt nach allen Filtern **niemand** übrig, läuft dieselbe Suche noch einmal, diesmal **mit**
Gefolgten. Lieber jemand Bekanntes als gar kein Ranking (und ein gerissener Streak). Alle anderen
Filter bleiben aktiv, auch die Meldungen. Ist auch dann niemand da: **404 „Aktuell sind keine
anderen Teilnehmer für das Ranking verfügbar.“**

### Gewichtete Auswahl: Wer hat bessere Chancen?

Aus allen erlaubten Kandidaten nimmt die Datenbank bis zu **200 zufällige**
(`TARGET_MAX_CANDIDATES`). Jeder davon bekommt ein **Gewicht**, dann wird gelost: Wer doppelt so
viel Gewicht hat, hat doppelt so große Chancen. Die Formel (`_target_gewicht`):

```
Gewicht = 1
        + 2   wenn wir mindestens einen Vibe teilen      (TARGET_VIBE_BONUS)
        + 2   wenn wir denselben Ort haben               (TARGET_LOCATION_BONUS)

Gewicht = Gewicht / (1 + wie oft er heute schon gezogen wurde, von allen zusammen)

Gewicht = Gewicht × 0,2   wenn er in den letzten 14 Tagen schon mein Target war
                          (TARGET_RECENT_FACTOR, TARGET_RECENT_DAYS)
```

Der Vibe-Bonus gibt es **einmal**, auch wenn beide Vibes passen.

**Ohne Personalisierung** (`personalized_feed = false`, siehe
[feed.md, Abschnitt 7](feed.md#7-personalisierung-abschalten-personalized_feed)) fallen Vibe-
und Orts-Bonus weg. Jeder Kandidat startet dann mit Gewicht 1. Die beiden Fairness-Teile
(„heute schon gezogen“, „kürzlich mein Target“) bleiben. Sie schützen andere User und sagen
nichts über meine Vorlieben voraus. Auch die Regel „Gefolgte nur in der Notlösung“ bleibt: Sie
schließt aus, sie bevorzugt niemanden nach Ähnlichkeit.

**Beispiel.** Ich habe die Vibes „Sport“ und „Musik“ und wohne in Berlin. Vier Kandidaten:

| Kandidat | Was zutrifft | Rechnung | Gewicht | Chance |
|---|---|---|---|---|
| A | Vibe „Sport“, auch Berlin, heute noch nicht gezogen | 1 + 2 + 2 | 5 | 72 % |
| B | nichts gemeinsam, heute schon 2× gezogen | 1 / (1 + 2) | 0,33 | 5 % |
| C | Vibe „Musik“, war vor 5 Tagen mein Target | (1 + 2) × 0,2 | 0,6 | 9 % |
| D | nichts gemeinsam | 1 | 1 | 14 % |

(Chance = eigenes Gewicht / Summe aller Gewichte, hier 6,93.)

**Warum so?**

- **Kein Gewicht wird 0.** Jeder behält eine Chance. Das ist bei wenigen Nutzern wichtig, sonst
  bekäme manch einer nie ein Ranking.
- **„Heute schon gezogen“ teilt.** Ohne das würden Leute mit beliebten Vibes in der Hauptstadt
  jeden Tag von allen gezogen, sammelten alle Punkte ein und das Leaderboard wäre immer gleich.
  Die Teilung verteilt die Aufmerksamkeit fairer.
- **„Kürzlich mein Target“** sorgt für Abwechslung, damit man nicht dreimal in der Woche
  dieselbe Person bekommt.
- **Die Follower-Zahl zählt bewusst nicht.** Das würde die ohnehin Bekannten noch bekannter
  machen.
- **Warum nur 200?** Bei sehr vielen Nutzern wäre es zu teuer, alle zu gewichten. 200 zufällige
  sind eine faire Stichprobe. Alles für die Gewichtung (Vibes, Ort, „heute gezogen“, „kürzlich
  mein Target“) kommt in **derselben Abfrage** mit (`_kandidaten_laden`).

Die Zahlen stehen alle in `app/ranking_config.py` und lassen sich dort drehen, ohne die Logik
anzufassen.

### Was die App zurückbekommt

`{"user_data": <Target>, "posts": [...]}`: die Posts des Targets aus den **letzten 7 Tagen**,
neueste zuerst, **höchstens 20**. Die 20 passt zur Obergrenze von `swipe_session` (dort sind
maximal 20 Swipes erlaubt). Es sind alle Posts dieser Zeit, nicht nur die aus dem
Profil-Ranking; die beiden Rankings sind voneinander unabhängig.

## 2. Target nachträglich gemeldet, blockiert oder gesperrt

Es kann passieren, dass das heutige Target **nach** der Ziehung problematisch wird:

- Ich melde mitten in der Session einen seiner Posts.
- Ich blockiere ihn, oder er mich.
- Ein Moderator sperrt ihn.

Dann soll ich ihn nie wieder sehen. Bei jedem Aufruf von `my_target` und `swipe_session` wird
das deshalb neu geprüft (`_target_nicht_mehr_erlaubt`, läuft als Spalte in derselben Abfrage
mit). Was dann passiert:

| Situation | Antwort |
|---|---|
| Heute **schon geswipt** | `my_target` → **404** „Dein heutiges Ranking ist bereits abgeschlossen.“ Das alte Target wird nicht mehr gezeigt. |
| Noch nicht geswipt, Neuziehungen übrig | Die Zeile in `daily_targets` wird **überschrieben** mit einem neuen Target, `rerolls` steigt um 1. |
| Noch nicht geswipt, schon **2 Neuziehungen** heute | `my_target` → **404** „Für heute sind keine weiteren Neuziehungen möglich.“ |
| `swipe_session` für ein solches Target | **404**, die Swipes werden nicht gespeichert. |

**Warum höchstens 2 Neuziehungen?** (`MAX_REROLLS_PER_DAY`) Ohne Grenze könnte man so lange
melden oder blockieren, bis jemand gezogen wird, dem man die Punkte geben will. Das wäre
Manipulation des Leaderboards.

**Warum gibt es keinen „Abbrechen“-Endpoint?** Ohne Meldung oder Block soll man das Target
**nicht** wechseln können. Ein Abbrechen-Knopf wäre genau das: ein Neu-Würfeln ohne Grund.

**Was die App tun muss (Vertrag mit dem Frontend):** Nach einer Meldung oder einem Block während
der Session verwirft die App die gesammelten Swipes, schickt `swipe_session` **nicht** ab und
ruft `GET /ranking/my_target` neu auf. Bei einem 404 aus `swipe_session` genauso.

## 3. Swipes abschicken (`POST /ranking/swipe_session`)

Body:

```json
{
  "target_user_id": 42,
  "swipes": [
    {"post_id": 101, "direction": true},
    {"post_id": 102, "direction": false}
  ]
}
```

### Prüfungen, in dieser Reihenfolge

1. Gibt es heute eine Ziehung für **genau dieses** Target, und ist es noch erlaubt? Sonst
   **404**. So kann man nicht einfach eine beliebige `target_user_id` schicken und jemandem
   Punkte schenken.
2. Heute schon eine Session abgeschlossen? → **409**. Eine Session pro Tag.
3. Ein Post doppelt im Paket? → **400**.
4. Ein Post existiert nicht? → **404**.
5. Ein Post gehört nicht dem Target? → **400**.

Außerdem erlaubt das Schema 1 bis 20 Swipes. Man muss **nicht** alle Posts swipen.

### Punkte

Wie viele Punkte ein Swipe bringt, hängt nur an zwei Dingen: der Richtung und dem **Flag** des
Posts (`posts.flag`, beim Posten wählbar):

| Flag | rechts (cool) | links (nicht cool) |
|---|---|---|
| kein Flag | 3 | 1 |
| `engagement` | 5 | 2 |
| `creativity` | 5 | 2 |
| `productivity` | 5 | 2 |

**Es gibt keine Minuspunkte.** Auch links bringt etwas. Wer etwas hochlädt, soll nie bestraft
werden. Ein Flag ist ein Bonus für Posts mit „Inhalt“.

**Beispiel.** Das Target hat 5 Posts, ich swipe:
2× `engagement` rechts (2 × 5 = 10), 1× ohne Flag rechts (3), 1× `creativity` links (2),
1× ohne Flag links (1). Das macht **16 Punkte** fürs Target. Die Antwort enthält außerdem eine
Aufschlüsselung nach Flag (`breakdown`).

### Was alles in einem Rutsch gespeichert wird

In **einem Commit** (alles oder nichts):

- je Swipe eine Zeile in `ranking_scores` (`voter_id`, `post_id`, `direction`, `points`).
  Das ist die **Wahrheit** über alle Punkte; Leaderboard-Fallback und Tagesbonus rechnen daraus.
- XP und Streak für mich, XP fürs Target (Details in
  [xp-streak-ligen.md](xp-streak-ligen.md)): ich bekomme 50 XP, das Target 1 XP pro Punkt.
- eine Zeile in `activities` fürs Target: `type = "rated"`, `payload = 16` (die Punkte). Eine
  Zeile pro Session, nicht pro Swipe. Das Target sieht sie über `GET /ranking/activity`
  (neueste 30).

**Danach**, außerhalb des Commits: die Punkte ins Live-Leaderboard in Redis (siehe unten).

Antwort (Status 201):

```json
{"success": true, "total_points": 16, "breakdown": {"none": 4, "engagement": 10, "creativity": 2, "productivity": 0},
 "message": "+16 Punkte!", "xp_gained": 50, "streak": 4}
```

## 4. Leaderboard (`GET /ranking/leaderboard`)

Liefert die **Top 7 von heute** und eine **„Du“-Zeile** mit dem eigenen Stand:

```json
{
  "entries": [{"target_user_id": 42, "username": "...", "profile_picture_url": "...",
               "total_points": 31, "total_ratings": 9}],
  "me": {"my_rank": 12, "my_points": 8, "points_to_next": 3}
}
```

- `total_points`: heute erhaltene Punkte.
- `total_ratings`: Anzahl der **einzelnen Swipes** auf seine Posts heute (nicht die Anzahl der
  Personen).
- `my_rank`: eigener Platz. Wer heute noch keine Punkte hat, steht hinter allen mit Punkten.
- `points_to_next`: wie viele Punkte bis zum Platz darüber fehlen (0 = schon Platz 1).

### Zwei Wege: Redis zuerst, SQL als Ersatz

**Normalfall Redis.** Für jeden Tag gibt es in Redis eine sortierte Liste
`leaderboard:2026-11-11` (Schlüssel = User-ID, Wert = Punkte). `swipe_session` erhöht dort die
Punkte des Targets (`ZINCRBY`). Ein Leaderboard-Abruf ist damit ~1 ms schnell, weil Redis die
Liste schon sortiert hat. Nur Name, Bild und `total_ratings` kommen aus einer SQL-Abfrage für
genau diese 7 Leute. Der Schlüssel verfällt nach 48 Stunden von selbst.

**Ersatz SQL.** Wirft der Redis-Weg irgendeinen Fehler, wird alles aus `ranking_scores` in SQL
zusammengezählt. Langsamer, aber immer korrekt.

**Warum überhaupt Redis?** Das Leaderboard wird oft abgerufen, und jedes Mal alle Swipes des
Tages zu summieren und zu sortieren wird mit wachsender Nutzerzahl teuer.

### Feinheiten, die man kennen sollte

- **Redis kann hinterherhinken.** Ist Redis während eines Swipes kurz weg, wird der Swipe
  trotzdem gespeichert (SQL ist die Wahrheit), aber die Punkte fehlen im Redis-Leaderboard
  dieses Tages. Läuft Redis danach wieder, merkt der Leaderboard-Abruf davon nichts und zeigt
  die zu niedrige Zahl. Der **Tagesbonus** ist nicht betroffen, er rechnet aus SQL.
- **Gleichstand.** Im SQL-Weg teilen sich Gleichstände denselben Platz. Redis vergibt dagegen
  immer eindeutige Plätze, bei gleicher Punktzahl entscheidet die Sortierung der IDs als Text.
- **Gelöschte Posts.** Löscht das Target einen geswipten Post, verschwinden dessen
  `ranking_scores`-Zeilen per CASCADE. SQL zählt die Punkte dann nicht mehr, Redis schon. Fehlen
  einem Top-7-Eintrag in Redis dadurch alle SQL-Zeilen, schlägt der Redis-Weg fehl und es greift
  automatisch der SQL-Weg.

## 5. Tagesende (`POST /ranking/end_of_day`)

Wird **nicht von der App** aufgerufen, sondern jede Nacht um **00:05 UTC** von einem pg_cron-Job
in Supabase (`ranking-end-of-day`). Geschützt mit dem Header `X-Ranking-Job-Secret` (Wert im
Supabase-Vault als `ranking_job_secret`), nicht mit Login. Er vergibt die Platzierungs-Boni für
**gestern**, Details in [xp-streak-ligen.md](xp-streak-ligen.md#tagesbonus-um-mitternacht).

## Datenmodell

| Tabelle | Inhalt | Wichtig |
|---|---|---|
| `daily_targets` | wer wen an welchem Tag bewertet; `rerolls` | Index `(target_user_id, date)` für „heute schon gezogen“ |
| `ranking_scores` | ein Swipe = eine Zeile mit fertigen Punkten | Quelle für SQL-Leaderboard und Tagesbonus |
| `activities` | Meldungen ans Target („du wurdest bewertet“) | auch von der Moderation genutzt |
| `daily_bonus_log` | ein Tag = eine Zeile, sobald der Bonus vergeben ist | verhindert doppelte Boni |
| Redis `leaderboard:<datum>` | Live-Punkte des Tages | verfällt nach 48 h |

## Bekannte Lücken und Stolpersteine

- **Datum lokal:** `my_target`, `swipe_session` und der Redis-Schlüssel nutzen `date.today()`
  (Rechnerzeit), der SQL-Fallback und `end_of_day` ausdrücklich UTC, und `daily_targets.date`
  setzt die Datenbank selbst (UTC). Auf Railway ist alles UTC und damit gleich. Lokal unter
  Windows in Deutschland gibt es zwischen 00:00 und 01:00/02:00 Uhr Abweichungen. Beim Testen
  in dieser Zeit nicht wundern.
- **Alter der Posts wird beim Swipen nicht geprüft.** `swipe_session` prüft nur, dass die Posts
  dem Target gehören, nicht dass sie aus den letzten 7 Tagen stammen. Die App bekommt aus
  `my_target` ohnehin nur solche. Ein eigener HTTP-Client könnte aber ältere Posts des Targets
  mitschicken. Die Punkte pro Swipe ändern sich dadurch nicht, nur die Auswahl.
- **`ranking_enabled` wird beim Swipen nicht erneut geprüft.** Wer das Ranking ausschaltet,
  nachdem er heute schon ein Target bekommen hat, kann die Session trotzdem noch abschicken.
- **Streak kann reißen,** wenn wirklich niemand verfügbar ist (404 bei `my_target`). Bei vielen
  Nutzern praktisch ausgeschlossen, bei sehr wenigen möglich.

## Was der Test prüft

`scripts/daily_target_test.py` (gegen die echte DB, wird zurückgerollt):

- Gefolgte, Blocks in beide Richtungen und Gesperrte werden nie gezogen; die Notlösung greift
  nur bei leerem Pool.
- Gemeldete werden nie gezogen, auch nicht als einziger Kandidat, in der Notlösung oder nach
  abgelehnter Meldung.
- Nachträglich gemeldet, blockiert oder gesperrt: vor dem Swipen neu ziehen, nach dem Swipen 404,
  nach 2 Neuziehungen 404.
- Gewichtung: Mit vielen Ziehungen im Speicher wird geprüft, dass Vibe und Ort öfter, „heute
  oft gezogen“ und „kürzlich Target“ seltener drankommen.
- Nie mehr als 20 Posts.

Ob die Boni ohne Personalisierung wegfallen, prüft `scripts/personalization_toggle_test.py`
(Block F).
