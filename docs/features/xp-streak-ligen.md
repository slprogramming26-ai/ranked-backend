# XP, Streak und Ligen

**Code:** `app/xp_config.py` (alle Werte und Rechenregeln), gebucht in `app/routers/ranking.py`,
angezeigt über `app/routers/user.py`
**Verwandt:** [tagesranking.md](tagesranking.md)

## Zwei Währungen

Im Ranking gibt es zwei verschiedene Zahlen, die man nicht verwechseln darf:

| | **Tagespunkte** | **XP** |
|---|---|---|
| Wofür | Wettkampf um den Tagessieg | Fortschritt auf Dauer |
| Wie lange | gelten nur einen Tag (UTC) | bleiben für immer, sinken nie |
| Wo gespeichert | `ranking_scores` + Redis-Leaderboard | Spalte `users.xp` |
| Wo sichtbar | Leaderboard | Profil (Liga, Fortschrittsbalken) |
| Werte in | `ranking_config.py` | `xp_config.py` |

Die Tagespunkte sind auf [tagesranking.md](tagesranking.md) erklärt. Hier geht es um alles,
was **dauerhaft** bleibt.

Wie bei den Punkten gilt: Die App rechnet **nichts** selbst. Sie liest XP, Streak und Liga nur
aus der Profil-Antwort ab.

## Woher XP kommen

| Quelle | Wer bekommt sie | Wie viel | Wann gebucht |
|---|---|---|---|
| Session abgeschlossen | der **Bewerter** | 50 (`XP_PER_SESSION`) | in `swipe_session` |
| Streak-Meilenstein | der **Bewerter** | 7 Tage: 100, 30 Tage: 500, 100 Tage: 2000 | in `swipe_session` |
| Punkte erhalten | das **Target** | 1 XP pro Tagespunkt (`XP_PER_POINT_RECEIVED`) | sofort in `swipe_session` |
| Platzierung am Tagesende | die **Top 10** von gestern | Platz 1: 200, Platz 2–3: 100, Platz 4–10: 50 | nachts in `end_of_day` |

**Warum bekommt der Bewerter überhaupt etwas?** Ohne die 50 XP wäre Bewerten reine Arbeit für
andere. Die Session-XP machen das tägliche Mitmachen selbst lohnend. Das ist der wichtigste Wert
im ganzen System.

**Beispiel.** Lisa bewertet heute Tom und gibt ihm 16 Punkte. Es ist ihr 7. Tag am Stück.

- Lisa: 50 (Session) + 100 (Meilenstein 7) = **150 XP**
- Tom: 16 Punkte × 1 = **16 XP**, sofort
- Landet Tom am Ende des Tages auf Platz 2, kommen nachts noch **100 XP** dazu.

**Profil-Ranking-Likes geben keine XP.** Das ist Absicht, siehe
[profil-ranking.md](profil-ranking.md).

## Streak

Der Streak zählt, an wie vielen Tagen **hintereinander** man eine Session abgeschlossen hat.
Gespeichert sind zwei Spalten am User: `streak_count` und `last_swipe_date` (der Tag der letzten
Session).

### Hochzählen (beim Abschicken der Session)

```
War meine letzte Session gestern?  → streak_count + 1
Sonst                              → streak_count = 1  (neu anfangen)
last_swipe_date = heute
```

Eine zweite Session am selben Tag gibt es nicht (409), deshalb muss der Fall „heute schon“ hier
nicht behandelt werden.

### Anzeigen (beim Profil-Abruf)

Der gespeicherte Streak wird **nur beim Swipen** geändert. Wer drei Tage nicht swipet, hat in
der Datenbank immer noch seinen alten Wert stehen. Deshalb rechnet die Anzeige live nach
(`get_effective_streak`):

```
last_swipe_date ist leer               → 0
last_swipe_date liegt vor gestern      → 0   (Streak ist gerissen)
sonst                                  → streak_count
```

**Beispiel:**

| Tag | Was passiert | gespeichert | angezeigt |
|---|---|---|---|
| Mo | Session | 1 | 1 |
| Di | Session | 2 | 2 |
| Mi | Session | 3 | 3 |
| Do | keine Session | 3 | **3** (gestern war ja noch eine, heute geht noch) |
| Fr | morgens, noch keine Session | 3 | **0** (Do fehlt) |
| Fr | Session | 1 | 1 |

### Meilensteine

Der Bonus kommt, wenn der Streak **genau** 7, 30 oder 100 erreicht. Bei 8 oder 14 gibt es
nichts. Reißt der Streak und erreicht später wieder 7, gibt es den 7er-Bonus **erneut**.

## Tagesbonus um Mitternacht

`POST /ranking/end_of_day` wird jede Nacht um **00:05 UTC** von einem pg_cron-Job in Supabase
aufgerufen (Name `ranking-end-of-day`, Header `X-Ranking-Job-Secret`, Wert aus dem Vault als
`ranking_job_secret`). Er arbeitet immer den **gestrigen UTC-Tag** ab. Mit `?day=2026-11-10`
kann man einen bestimmten Tag angeben.

Ablauf:

1. Steht der Tag schon in `daily_bonus_log`? → **409**, nichts passiert.
2. Alle Targets des Tages nach Punkten absteigend sortieren, gerechnet aus `ranking_scores`
   (**nicht** aus Redis, SQL ist die Wahrheit).
3. Von oben nach unten Platz für Platz Bonus-XP gutschreiben, bis Platz 10.
4. Eine Zeile in `daily_bonus_log` schreiben, **im selben Commit** wie die XP.

**Warum der Log?** Er macht den Job sicher gegen doppeltes Ausführen. Läuft der Job zweimal für
denselben Tag, bekommt der zweite Aufruf 409. Laufen beide exakt gleichzeitig, scheitert der
zweite Commit am Primärschlüssel (`date`) und wird zurückgerollt. XP können also nicht doppelt
vergeben werden.

> In [`SECRET_ROTATION.md`](../../SECRET_ROTATION.md) steht, man solle `ranking-end-of-day` nie
> von Hand auslösen. Nach dem aktuellen Code wäre ein zweiter Aufruf für denselben Tag harmlos
> (409). Vorsichtig zu bleiben schadet trotzdem nicht. Ob der Job gelaufen ist, sieht man am
> Morgen danach in `net._http_response` (die Antworten bleiben dort nur etwa 6 Stunden).

**Gleichstand:** Anders als im SQL-Leaderboard teilen sich Gleichstände hier **keinen** Platz.
Haben Platz 3 und 4 gleich viele Punkte, bekommt einer 100 und der andere 50 XP. Wer von beiden
vorne liegt, entscheidet zufällig die Reihenfolge, in der die Datenbank sie liefert.

## Ligen

Die Liga wird **nicht gespeichert**, sondern bei jedem Profil-Abruf aus `users.xp` berechnet
(`get_league`). Dadurch kann man Schwellen und Namen in `xp_config.py` ändern, ohne Daten
nachzuziehen.

| ab XP | Liga | `tier` |
|---|---|---|
| 0 | Bronze | 0 |
| 500 | Silber | 1 |
| 1.500 | Gold | 2 |
| 3.500 | Platin | 3 |
| 7.000 | Diamant | 4 |
| 12.000 | Meister | 5 |
| 20.000 | Legende | 6 |

Die Antwort (`league` in `GetUserOut`) enthält alles für Ring und Fortschrittsbalken.
**Beispiel** bei 2.300 XP:

```json
{"league": "Gold", "tier": 2, "next_league": "Platin",
 "xp_into_league": 800, "league_span": 2000, "xp_for_next": 1200}
```

Gelesen: „Gold, 800 von 2000, noch 1200 bis Platin“. In der höchsten Liga ist `next_league`
leer und `league_span` sowie `xp_for_next` sind 0.

## Wo die Werte in der App ankommen

`GET /users/` (eigenes Profil) und `GET /users/{id}` (fremdes Profil) liefern `xp`,
`streak_count` (schon der „angezeigte“ Wert, siehe oben) und `league`. Die Nutzersuche
`GET /users/search` liefert sie **bewusst nicht**, dort bleiben die Felder leer.
`POST /ranking/swipe_session` liefert direkt nach der Session `xp_gained` und den neuen `streak`.
