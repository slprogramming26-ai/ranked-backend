# Profil-Ranking

**Code:** `app/routers/profile_ranking.py` (Prefix `/profile_ranking`), Frist in
`app/ranking_config.py`, Profilzahl in `app/routers/user.py`
**Tests:** `scripts/profile_ranking_test.py`
**Verwandt:** [tagesranking.md](tagesranking.md)

## Worum es geht

Das [Tagesranking](tagesranking.md) ist nach 10–20 Sekunden für den Tag erledigt. Das
Profil-Ranking gibt dem Ranking einen zweiten, ruhigeren Teil:

- Im Profil gibt es neben **Posts** und **Stories** einen Tab **Ranking**.
- Der Besitzer stellt dort eigene Posts hinein, **höchstens einen pro Tag**.
- **Follower** können diese Posts eine Woche lang swipen: rechts = Like, links = überspringen.
- Sichtbar ist **nur die Like-Zahl**, am Post und als Gesamtsumme im Profil. Wer übersprungen
  hat, sieht niemand, und Views gibt es nicht.

Die beiden Rankings sind **komplett getrennt**:

| | Tagesranking | Profil-Ranking |
|---|---|---|
| Wer wird bewertet | eine fremde Person, gezogen | Leute, denen ich folge |
| Wie oft | eine Session pro Tag | jederzeit, ein Swipe pro Post |
| Welche Posts | alle der letzten 7 Tage | nur die vom Besitzer hineingestellten |
| Ergebnis | Tagespunkte, Leaderboard, XP, Streak | nur Likes |
| Tabelle | `ranking_scores` | `profile_ranking_swipes` |

**Profil-Likes geben keine Leaderboard-Punkte, keine XP und keinen Streak** und schreiben
nichts in `ranking_scores`. Grund: Wer viele Follower hat, würde sonst automatisch das
Leaderboard und die Ligen dominieren. Das Tagesranking soll fair bleiben und Neues zeigen.

## Die Regeln auf einen Blick

1. **1 Post pro Tag hinein.** Beim Posten oder bis 7 Tage danach.
2. **Nicht wieder heraus.** Herausnehmen geht nicht, nur den ganzen Post löschen.
3. **7 Kalendertage swipebar**, gerechnet ab dem Tag des Hineinstellens (UTC).
4. Daraus folgt: **höchstens 7 aktive Posts** gleichzeitig. Einen eigenen Zähler braucht es
   dafür nicht.
5. **Nur Follower** dürfen swipen. Nie den eigenen Post, nie bei einem Block (beide Richtungen).
6. **Ein Swipe pro Person und Post**, ohne Rücknahme.
7. Nach den 7 Tagen ist der Post **nur noch ansehbar**, seine Likes sind eingefroren.
8. Die **Gesamtzahl im Profil** zählt alle Likes aller Ranking-Posts, auch der abgelaufenen.
   Sie sinkt nur, wenn ein Post gelöscht wird oder ein Liker sein Konto löscht.

## Die 7-Tage-Frist an einem Beispiel

Es gibt **zwei** Fristen, beide 7 Tage (`PROFILE_RANKING_DAYS`), aber unterschiedlich gerechnet:

**a) Bis wann darf ich einen Post hineinstellen?** Rollend nach Uhrzeit, ab `created_at`.
Gepostet am 2026-11-04 um 18:00 UTC → hineinstellen geht bis 2026-11-11 um 18:00 UTC. Danach
**400 „Dieser Post ist zu alt für das Ranking.“**

**b) Wie lange ist er swipebar?** In ganzen Kalendertagen, ab `ranking_entered_on` (UTC-Datum
beim Hineinstellen):

```
hineingestellt am 2026-11-11

  11.  12.  13.  14.  15.  16.  17. │ 18.  19.  ...
  ───────── swipebar (7 Tage) ───── │ abgelaufen, nur ansehen
```

Im Code ist das überall dieselbe Grenze:

```
aktiv       ⇔  ranking_entered_on  >  heute − 7 Tage
abgelaufen  ⇔  ranking_entered_on  <= heute − 7 Tage
```

Am 18.11. ist `heute − 7 Tage` = 11.11., und 11.11. > 11.11. ist falsch → abgelaufen. Am 17.11.
ist es 10.11., und 11.11. > 10.11. stimmt → noch aktiv. `/open`, `/swipes` und `/posts`
benutzen genau diesen Vergleich. Wer ihn ändert, muss alle drei ändern.

## Ablauf in der App

```
Besitzer                                     Follower
  │ POST /profile_ranking/posts/{post_id}       │
  │   → Post ist im Ranking                     │
  │                                             │ GET /profile_ranking/users/{id}/open
  │                                             │   → can_swipe + noch nicht geswipte Posts
  │                                             │ POST /profile_ranking/swipes   (je Post einmal)
  │                                             │   → gespeichert, bei Like Zähler +1
  │                                             │
  Jeder: GET /profile_ranking/users/{id}/posts  → Ranking-Tab mit Like-Zahlen
         GET /users/{id}                        → ranking_likes_total im Profil
```

## 1. Post hineinstellen (`POST /profile_ranking/posts/{post_id}`)

Kein Body. Prüfungen in dieser Reihenfolge:

| Status | Wann |
|---|---|
| 404 | Post existiert nicht |
| 403 | Post gehört nicht mir |
| 409 | Post ist schon im Ranking |
| 400 | Post ist älter als 7 Tage (Frist a) |
| 409 | Ich habe heute schon einen Post hineingestellt |
| **201** | `{"post_id": 7, "ranking_entered_on": "2026-11-11"}` |

**Die Tagessperre** steht als Datum am User: `users.last_ranking_entry_on`. Sie wird nicht erst
gelesen und dann geschrieben, sondern mit **einer** Anweisung geprüft und gesetzt:

```sql
UPDATE users SET last_ranking_entry_on = heute
WHERE id = ich AND (last_ranking_entry_on IS NULL OR last_ranking_entry_on < heute)
```

Ändert das 0 Zeilen, war der Platz heute schon belegt → 409. **Warum so?** Tippt jemand doppelt
oder schickt zwei Posts gleichzeitig, kämen bei „erst lesen, dann schreiben“ beide durch die
Prüfung. Beim UPDATE sperrt die Datenbank die Zeile, die zweite Anfrage wartet und ändert danach
0 Zeilen.

**Warum ein eigener Endpoint statt eines Felds beim Posten?** `PUT /posts/{id}` schreibt beim
Bearbeiten das ganze `PostCreate` zurück. Ein Ranking-Feld darin könnte beim Bearbeiten
versehentlich überschrieben werden. Die App ruft nach dem Erstellen des Posts (wenn der Schalter
an ist) einfach zusätzlich diesen Endpoint auf.

**Kein `ranking_enabled` nötig.** Das Profil-Ranking ist unabhängig vom Tagesranking-Schalter.

**Datenmodell:** Es gibt keine eigene Liste „Posts im Ranking“. Ein Post ist im Ranking, wenn
`posts.ranking_entered_on` gesetzt ist. Alle Abfragen laufen über den Index auf
`posts.owner_id`, und ein Nutzer hat nicht so viele Posts, dass das teuer wird.

## 2. Was kann ich swipen? (`GET /profile_ranking/users/{user_id}/open`)

Liefert `{"can_swipe": bool, "posts": [...]}`.

`can_swipe = false` und eine **leere Liste**, wenn:

- es mein eigenes Profil ist (ohne Datenbank-Abfrage),
- ich dem Besitzer nicht folge,
- ein Block besteht, egal in welche Richtung,
- der Besitzer gerade gesperrt ist.

Gibt es den User nicht: **404**.

Sonst `can_swipe = true` und die Posts, die

- noch **aktiv** sind (Frist b),
- ich **noch nicht** geswipt habe (auch nicht übersprungen),
- ich **nicht gemeldet** habe.

Sortiert: **was zuerst abläuft, kommt zuerst** (`ranking_entered_on` aufsteigend). Höchstens 7
Posts, deshalb ohne Seiten. Hat man alles geswipt, kommt `can_swipe = true` mit leerer Liste.

**Bewusst ohne Like-Zahl.** Die Posts kommen im normalen Post-Format ohne `ranking_like_count`.
Man soll erst swipen und dann sehen, wie andere es fanden, nicht der Masse folgen.

Technisch sind das zwei Abfragen: zuerst „gibt es den User, folge ich, Block, gesperrt?“ in
einer Anweisung, die Posts nur, wenn ich swipen darf.

## 3. Swipen (`POST /profile_ranking/swipes`)

Body: `{"post_id": 101, "direction": true}` (true = Like, false = überspringen).
**Ein Swipe pro Anfrage.** Anders als im Tagesranking gibt es kein Paket. Grund: Es gibt keine
Punkte und keine Tagesaufgabe. Bricht jemand ab, geht nichts verloren, und ein Post, der genau
in dem Moment abläuft, kippt nicht die anderen Swipes mit.

Prüfungen in dieser Reihenfolge:

| Status | Wann |
|---|---|
| 404 | Post existiert nicht oder ist nicht im Ranking |
| 403 | eigener Post |
| 403 | ich folge dem Besitzer nicht |
| 403 | Block, egal in welche Richtung |
| 409 | Frist abgelaufen |
| 409 | diesen Post habe ich schon geswipt |
| **201** | `{"post_id": 101, "liked": true}` |

Post, „folge ich“ und „Block“ kommen in **einer** Abfrage (`_post_mit_pruefungen`).

**Doppelte Swipes** verhindert die Datenbank selbst: `profile_ranking_swipes` hat den
Primärschlüssel `(voter_id, post_id)`. Das Insert lautet „einfügen, bei Konflikt nichts tun“
(`ON CONFLICT DO NOTHING ... RETURNING`). Kommt nichts zurück, gab es die Zeile schon → 409.
Das ist auch bei zwei gleichzeitigen Anfragen sicher, weil die Datenbank entscheidet und nicht
Python.

**Bei Like** steigt `posts.ranking_like_count` um 1, **im selben Commit** wie das Insert. Beim
Überspringen ändert sich keine Zahl.

## 4. Ranking-Tab ansehen (`GET /profile_ranking/users/{user_id}/posts?limit=10&skip=0`)

Alle Ranking-Posts eines Profils, aktive und abgelaufene, **nur zum Ansehen**. `limit` 1–50
(Standard 10), `skip` ab 0 zum Nachladen.

| Status | Wann |
|---|---|
| 404 | User existiert nicht |
| 403 | Block, egal in welche Richtung |

Anders als bei `/open`: **keine Follower-Pflicht**, und ein gesperrter Besitzer wird **normal**
angezeigt. Das verhält sich damit wie die normalen Posts im Profil. Selbst gemeldete Posts sind
ausgeblendet.

Sortierung: meiste Likes zuerst, bei Gleichstand der neuere, zuletzt nach ID. Die ID am Ende
macht die Reihenfolge eindeutig; sonst könnten beim Nachladen mit `skip` Posts doppelt kommen
oder fehlen.

Jeder Eintrag:

```json
{
  "post": { ... normaler Post ... },
  "ranking_like_count": 12,
  "ranking_entered_on": "2026-11-11",
  "is_active": true,
  "my_swipe": true
}
```

- `is_active`: noch in der Frist, **vom Server in UTC** berechnet. Die App soll das nicht selbst
  aus dem Datum ausrechnen (Zeitzone des Handys).
- `my_swipe`: `true` = ich habe geliked, `false` = übersprungen, `null` = nicht geswipt.
  Übersprungen-Zeilen werden nach 14 Tagen gelöscht (siehe unten), danach steht dort bei sehr
  alten Posts wieder `null`.

## 5. Die Zahl im Profil (`ranking_likes_total`)

`GET /users/` und `GET /users/{id}` liefern `ranking_likes_total`: die Summe von
`ranking_like_count` über **alle** Posts des Users, auch abgelaufene, 0 wenn es keine gibt.

Sie kommt als skalare Unterabfrage **in derselben Anweisung** wie Nutzer, Follower-Zahl und
„folge ich“ mit. `GET /users/{id}` braucht dadurch insgesamt nur noch 1 statt vorher 3
Datenbank-Rundwege.

## Der Like-Zähler muss stimmen

`posts.ranking_like_count` ist ein mitgeschriebener Zähler (Regel 4 in der
[README](../README.md)). Er soll **immer** gleich der Anzahl der Zeilen in
`profile_ranking_swipes` mit `liked = true` für diesen Post sein. Diese Stellen halten das ein:

| Ereignis | Was passiert | Wo |
|---|---|---|
| Like | +1 im selben Commit | `POST /swipes` |
| Überspringen | nichts | |
| Besitzer löscht den Post | Post und Swipes weg (CASCADE), Profilsumme sinkt von selbst | |
| **Liker löscht sein Konto** | **vorher** −1 auf jedem Post, den er geliked hat, auch auf abgelaufenen; erst danach räumt CASCADE seine Swipes weg | `_delete_user_data` in `user.py`, Schritt 3b |
| Moderator entfernt den Post | Post samt Swipes wandert ins Archiv `removed_content` | `admin.py`, `POST_ARCHIVE_SQL` |
| Moderator stellt ihn wieder her | Post und Swipes zurück, **Zähler neu gezählt** | `admin.py`, `_restore_post` |
| Follower entfolgt | nichts, sein Like bleibt | |
| Cleanup der Überspringen-Zeilen | nichts, weil nur `liked = false` gelöscht wird | `cleanup.py` |

**Warum beim Restore neu zählen?** Zwischen Entfernen und Wiederherstellen können Liker ihr
Konto gelöscht haben. Deren Swipes kommen nicht zurück (sonst würde der Fremdschlüssel den
ganzen Restore scheitern lassen). Der alte Zählerstand im Archiv wäre dann zu hoch.

**Alte Archivzeilen:** Posts, die vor Einführung des Profil-Rankings archiviert wurden, haben im
JSON noch keine Spalte `ranking_like_count`. Ohne Gegenmaßnahme würde daraus NULL, und die
Spalte ist NOT NULL → der Restore würde scheitern. Deshalb setzt der Restore zuerst
`{"ranking_like_count": 0}` als Standard und legt den Archiv-Post darüber.

## Aufräumen: Überspringen-Zeilen

`DELETE /cleanup/profile_ranking_skips` löscht Zeilen mit `liked = false`, die älter als
**14 Tage** sind (`PROFILE_RANKING_SKIP_RETENTION_DAYS`). Aufgerufen täglich um **03:40 UTC** vom
pg_cron-Job `clear-profile-ranking-skips` (Header `X-Cleanup-Secret`, Wert aus dem Vault als
`story_cleanup_secret`).

**Warum dürfen die weg?** Eine Überspringen-Zeile braucht man nur für `/open` („hat er schon
gesehen“). Geswipt wird immer innerhalb der 7-Tage-Frist, nach spätestens 7 weiteren Tagen ist
der Post sicher abgelaufen. 14 Tage sind doppelter Puffer. **Likes bleiben für immer**, denn sie
sind die Grundlage des Zählers.

## Datenmodell

| Wo | Feld | Zweck |
|---|---|---|
| `posts` | `ranking_entered_on` (DATE, leer = nicht im Ranking) | im Ranking ja/nein + Start der Frist |
| `posts` | `ranking_like_count` (INT, Standard 0) | Like-Zähler |
| `users` | `last_ranking_entry_on` (DATE) | Tagessperre „1 Post pro Tag“ |
| `profile_ranking_swipes` | `voter_id` + `post_id` (zusammen Primärschlüssel), `liked`, `created_at` | ein Swipe; beide Fremdschlüssel mit CASCADE, Index auf `post_id` |

## Bekannte Lücken

- **`/swipes` prüft keine Sperre des Besitzers und keine eigene Meldung.** `/open` blendet
  beides aus, die App bekommt solche Posts also nicht angeboten. Ein eigener HTTP-Client könnte
  sie aber trotzdem swipen.
- **Meldungen wirken hier anders als im Tagesranking.** Im Tagesranking schließt **jede**
  Meldung gegen eine Person diese Person aus. Im Profil-Ranking verschwindet nur der **gemeldete
  Post**; die anderen Posts derselben Person bleiben sichtbar.

## Vor dem Launch

- Datenschutzerklärung: die Speicherung der Swipes (auch Überspringen, 14 Tage) erwähnen.
- Der geplante Datenexport muss `profile_ranking_swipes` umfassen.
- Keine gegenseitige „beide haben geliked → Chat frei“-Funktion bauen und „Swipe“ nicht im
  sichtbaren Namen verwenden. Grund: Das US-Patent 9,733,811 deckt genau so ein gegenseitiges
  Freischalten ab, und „Swipe“ ist markenrechtlich heikel. Vor einem US-Launch einen
  Patentanwalt fragen.

## Was der Test prüft

`scripts/profile_ranking_test.py` (gegen die echte DB, wird zurückgerollt):

- jeder Fehlerfall aller vier Endpoints in der richtigen Reihenfolge,
- zweiter Post am selben Tag abgelehnt, am Folgetag wieder erlaubt,
- 7-Tage-Grenze am ersten und am letzten Tag,
- ein Post verschwindet aus `/open`, nachdem man ihn geswipt hat,
- Zähler nach Like, Überspringen, Konto-Löschung eines Likers, Post-Löschung und Cleanup,
- keine neue Zeile in `ranking_scores`, XP und Streak unverändert,
- `ranking_likes_total`: nur Ranking-Likes, 0 statt leer, eine Anweisung.

Archiv und Restore prüfen `scripts/content_archive_test.py` und
`scripts/content_restore_test.py`: Swipes kommen zurück, der Zähler wird neu gezählt, und ein
Restore im alten Archivformat (ohne `ranking_like_count`) klappt.
