# Backend-Doku

Diese Doku erklärt die **großen Zusammenhänge** im Backend: welche Regeln gelten, wie ein
Ablauf von Anfang bis Ende durchläuft und **warum** etwas so gebaut ist. Sie ist kein
Funktionsverzeichnis.

- **Einzelne Funktionen** erklären ihre Docstrings und Kommentare im Code.
- **Alle Endpoints mit Feldern** zeigt FastAPI selbst unter der URL `/docs` (Swagger), wenn
  der Server läuft. Das hat nichts mit diesem Ordner zu tun, es heißt nur zufällig gleich.

Wenn Code und Doku sich widersprechen, gilt der Code. Dann bitte die Doku nachziehen.

## Seiten

### Ranking

| Seite | Worum es geht |
|---|---|
| [features/tagesranking.md](features/tagesranking.md) | Einmal am Tag eine fremde Person bewerten: wer gezogen wird, wie die Punkte entstehen, Leaderboard |
| [features/xp-streak-ligen.md](features/xp-streak-ligen.md) | Die zweite Währung: XP, Streak, Tagesbonus um Mitternacht, Ligen |
| [features/profil-ranking.md](features/profil-ranking.md) | Ranking-Tab im Profil: eigene Posts hineinstellen, Follower liken, Likes zählen dauerhaft |

### Feed

| Seite | Worum es geht |
|---|---|
| [features/feed.md](features/feed.md) | Startseite: welche Posts, Score-Formel, Lokal-Feed, Suche, eine Anweisung pro Seite, Reihenfolge-Cache |
| [features/lytir.md](features/lytir.md) | Das Ranking-Modell: Impressions sammeln, Features, Label, Training, Ranker, Live schalten |

### Geplant (noch nicht geschrieben)

In dieser Reihenfolge:

1. **WebSocket-Architektur:** Go-Gateway als „dummes Rohr“, Weg einer Nachricht, Presence.
   Das Protokoll selbst steht schon in [`PROTOCOL.md`](../PROTOCOL.md) im Repo-Root.
2. **Schlüssel / E2EE:** öffentliche Schlüssel, Key-Backup, Gruppen-Epochen und Rekey
3. **Moderation:** Rollen, Sperren, Meldungen, Archiv entfernter Inhalte, Audit-Log
4. **Betrieb:** Migrationen, Cron-Jobs, Secrets. Die Secret-Rotation steht schon in
   [`SECRET_ROTATION.md`](../SECRET_ROTATION.md).

## Regeln, die überall im Backend gelten

Diese Punkte tauchen auf fast jeder Seite wieder auf. Sie stehen einmal hier, damit man sie
nicht überall neu erklären muss.

### 1. Der Server rechnet, der Client schickt nur Absichten

Die App schickt zum Beispiel nur „nach rechts geswipt“. Wie viele Punkte oder XP das wert ist,
entscheidet ausschließlich der Server. Wer bewertet, kommt immer aus dem Login-Token und nie
aus dem Request-Body. Sonst könnte jeder mit einem eigenen HTTP-Client sich selbst Punkte geben
oder im Namen anderer bewerten.

### 2. Datenbank-Rundwege sind teuer, Zeilen fast gratis

Die Datenbank (Supabase) steht nicht im selben Rechenzentrum wie der Server (Railway). Jede
einzelne SQL-Anweisung kostet deshalb grob **15 ms Wartezeit**, egal ob sie 1 oder 150 Zeilen
liefert. Darum werden Prüfungen wie „folge ich dem?“ oder „gibt es einen Block?“ möglichst als
**zusätzliche Spalte in dieselbe Abfrage** gepackt (`EXISTS`, skalare Unterabfrage), statt eine
eigene Abfrage zu schicken. Wo im Code „in EINER Anweisung“ steht, ist das der Grund.

### 3. Zeit ist UTC

Tage („heute“, „gestern“, „7 Tage Frist“) werden in **UTC** gerechnet. In Deutschland beginnt
der Ranking-Tag also um 01:00 Uhr (Winter) bzw. 02:00 Uhr (Sommer). Der Server auf Railway läuft
selbst in UTC. **Achtung lokal:** Ein paar Stellen nutzen `date.today()` (Uhrzeit des Rechners)
statt ausdrücklich UTC. Auf Railway ist das dasselbe, auf einem Windows-PC in Deutschland
zwischen Mitternacht und 01:00/02:00 Uhr nicht. Das ist auf den einzelnen Seiten markiert.

### 4. Zähler werden mitgeschrieben statt gezählt

Wo eine Zahl oft angezeigt wird (Likes eines Posts, Ranking-Likes), steht sie als fertige
Spalte am Post (`vote_count`, `ranking_like_count`) und wird bei jedem Like um 1 erhöht. Das
spart das Zählen bei jedem Abruf. Der Preis: Überall, wo Likes verschwinden (Konto gelöscht,
Post wiederhergestellt), muss der Zähler von Hand korrigiert werden. Auf den Seiten steht
jeweils, wo das passiert.

### 5. Gesperrte Nutzer

Läuft eine Sperre (`banned_until` liegt in der Zukunft), weist schon der Login-Check
(`oauth2.get_current_user`) jede Anfrage mit **403 `account_suspended`** ab. Gesperrte können
also selbst nichts tun. Wo andere Nutzer einen Gesperrten **sehen** könnten (zum Beispiel als
Tagesranking-Target), muss die jeweilige Abfrage ihn zusätzlich herausfiltern. Eine abgelaufene
Sperre behält ihr Datum, deshalb heißt der Filter immer „`banned_until` ist leer **oder** liegt
in der Vergangenheit“, nicht nur „ist leer“.

### 6. Blocks gelten in beide Richtungen

Wenn A den B blockiert hat, sollen sich beide nicht mehr begegnen. Deshalb prüfen die Abfragen
immer beide Richtungen: „ich habe ihn blockiert“ **oder** „er hat mich blockiert“.

## Tests

Die meisten Features haben ein Testskript in `scripts/`. Diese Skripte laufen **gegen die echte
Datenbank** (die lokale `.env` zeigt auf Supabase-Produktion), aber komplett in einer
Transaktion, die am Ende zurückgerollt wird. Es bleibt also nichts zurück. Start immer aus dem
Repo-Root, zum Beispiel:

```powershell
.\venv\Scripts\python.exe scripts\daily_target_test.py
```

Dazu kommt `pytest tests/` mit einem SQL-Snapshot-Test: Er vergleicht das von SQLAlchemy erzeugte
SQL mit `tests/sql_snapshot.sql`. Ändert sich eine Abfrage absichtlich, Diff prüfen und Snapshot
neu schreiben.
