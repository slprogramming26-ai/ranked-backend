# Secret-Rotation: Notfall-Anleitung

**Stand:** 2026-10-25 · **Anlass:** Railway-Einbruch vom 20.–22.10.2026

Wenn ein Hoster, ein Laptop oder ein Chatverlauf Secrets verloren haben könnte, arbeitest du
diese Datei von oben nach unten ab. Beim ersten Mal hat das einen Nachmittag gedauert. Mit
dieser Anleitung sollte es in einer Stunde gehen.

**Grundregel:** Ein gestohlenes Secret wird erst wertlos, wenn der **alte Wert nirgends mehr
gilt**. Den neuen Wert einzutragen reicht nicht. Alte Keys müssen widerrufen werden.

---

## 1. Was ist passiert (Oktober 2026)

Ein Angreifer war vom 20.10. 13:58 UTC bis zum 22.10. 03:32 UTC in Railways internen Systemen.
Er hat die verschlüsselten Umgebungsvariablen der Kunden abgefragt und einen Teil davon
entschlüsselt. Railway hat das zuerst als „Platform Outage“ gemeldet. Erst später kam heraus,
dass es ein Einbruch war. Railway hat uns direkt angeschrieben.

Quellen: [Railway-Bericht](https://blog.railway.com/p/preliminary-report-october-security-incident),
[Changelog](https://railway.com/changelog/2026-10-23-october-21-security-incident).

**Lehre:** Bei einer „technischen Störung“ des Hosters nachlesen, was genau passiert ist.

---

## 2. Bestandsaufnahme: wo liegt was

| Secret | Railway Backend | Railway Gateway | Lokal | Sonst wo | Schaden bei Verlust |
|---|---|---|---|---|---|
| `DATABASE_PASSWORT` | ✓ | – | Backend-`.env` | Supabase | **Alle Daten lesen und ändern** |
| `SECRET_KEY` | ✓ | ✓ **gleich** | beide `.env` | – | **Tokens für jeden Nutzer fälschen, auch für Admins** |
| `WS_INTERNAL_SECRET` | ✓ | ✓ **gleich** | beide `.env` | – | Als jeder Nutzer Nachrichten schreiben |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | ✓ | – | Backend-`.env` | Supabase Storage | Bilder lesen, ersetzen, löschen |
| `REDIS_URL` (enthält Passwort) | Referenz | Referenz | – (lokal: localhost) | Redis-Service | Leaderboard, Presence, Limiter |
| `STORY_CLEANUP_SECRET` | ✓ | – | Backend-`.env` | Vault `story_cleanup_secret` | 5 Cleanup-Endpoints auslösen |
| `RANKING_JOB_SECRET` | ✓ | – | Backend-`.env` | Vault `ranking_job_secret` | Tagesabschluss auslösen (doppelte XP) |
| `GROUP_JOIN_CODE_CLEANUP_SECRET` | ✓ | – | Backend-`.env` | Vault `group_join_code_cleanup_secret` | Join-Codes löschen |
| `LOCATION_ADMIN_SECRET` | ✓ | – | Backend-`.env` | – | Städte anlegen |

Nicht geheim und nicht zu rotieren: `ALGORITHM`, `S3_REGION`, `S3_ENDPOINT`, `SUPABASE_URL`,
`BACKEND_URL`.

Die Chatinhalte sind Ende-zu-Ende-verschlüsselt. Selbst mit DB-Zugriff kann niemand sie lesen.

---

## 3. Werte erzeugen

```powershell
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

- **Für jedes Secret einen eigenen Wert erzeugen.** Beim ersten Durchlauf hatten zwei Secrets
  aus Versehen denselben Wert, das Prüfskript fängt das jetzt ab.
- Werte nie in einen Chat, nie in Git, nie in Screenshots.
- Die `.env` nach dem Einfügen **speichern** (Strg+S). Eine ungespeicherte `.env` hat uns einen
  Prüfdurchlauf gekostet.

---

## 4. Reihenfolge

Die Reihenfolge richtet sich nach dem Schaden bei Verlust. Bei jedem Schritt gilt: **Railway,
lokal und, wo nötig, Supabase oder Vault gleichzeitig nachziehen.**

### 4.1 DB-Passwort

1. Supabase → Project Settings → Database → **Reset database password**.
2. Railway, Backend: `DATABASE_PASSWORT` setzen, dann **Deploy** klicken. Railway sammelt
   Änderungen und übernimmt sie erst mit diesem Klick.
3. Lokale Backend-`.env` anpassen. alembic und `scripts/backup_db.py` brauchen den Wert.

pg_cron braucht das Passwort nicht, denn es läuft innerhalb der DB.

### 4.2 `SECRET_KEY` und `WS_INTERNAL_SECRET`

1. Zwei Werte erzeugen.
2. Beide Werte bei Railway in **Backend und Gateway** eintragen, jeweils exakt gleich. Dann beide
   Services deployen.
3. Beide Werte lokal in **beide** `.env` eintragen (`ranked_backend_api` und `ws-gateway`).

Es wird niemand ausgeloggt. Die Refresh Tokens sind Zufallsstrings in der DB und hängen nicht am
`SECRET_KEY`. Die App holt sich still einen neuen Access Token.

**Falle:** Wenn ein Test gegen Produktion mit 401 oder Close 1008 scheitert, liegt das fast
immer daran, dass lokal noch der alte Wert steht. Railway ist dann meist schon richtig. Zuerst
das Prüfskript ausführen, es vergleicht die Fingerabdrücke.

### 4.3 S3 (Supabase Storage)

1. Supabase → Storage → S3 Configuration → **New access key**. Das Secret wird nur einmal
   angezeigt.
2. Railway, Backend: `S3_ACCESS_KEY` und `S3_SECRET_KEY` setzen, deployen. Lokale Backend-`.env`
   anpassen.
3. In der App ein Bild hochladen.
4. **Erst dann den alten Key löschen.** Wer das vergisst, hat nichts rotiert.

### 4.4 Redis

1. Redis-Service → Tab **Config** → **Regenerate password**.
   **Nicht** die Variable `REDIS_PASSWORD` von Hand ändern. Dann ändert sich nur die Variable,
   der Server erwartet weiter das alte Passwort. Railway warnt davor.
2. Backend und Gateway neu deployen. Sie holen `REDIS_URL` über die Referenz
   `${{Redis.REDIS_URL}}`, und Referenzen werden erst beim Deploy neu gelesen.
3. Redis-Service → Settings → Networking → **Public Networking: TCP-Proxy entfernen**. Am
   2026-10-25 entfernt, nichts braucht ihn. Falls er wieder auftaucht: Redis gehört nicht ins
   offene Internet.

### 4.5 Cron- und Admin-Secrets

Die pg_cron-Jobs lesen ihre Secrets aus dem **Supabase Vault**. Die Jobs selbst muss man beim
Rotieren nicht anfassen.

1. Vier Werte erzeugen: Story, Ranking, Join-Code, Location.
2. Railway, Backend: alle vier setzen, deployen. Lokale Backend-`.env` anpassen.
3. Supabase SQL Editor, die drei Cron-Secrets im Vault ersetzen:
   ```sql
   select vault.update_secret((select id from vault.secrets where name = 'story_cleanup_secret'), '<neu>');
   select vault.update_secret((select id from vault.secrets where name = 'ranking_job_secret'), '<neu>');
   select vault.update_secret((select id from vault.secrets where name = 'group_join_code_cleanup_secret'), '<neu>');
   ```
4. **Zeitfenster beachten:** Die Jobs laufen um 00:05 UTC (Ranking) und zwischen 03:00 und
   03:55 UTC (Cleanup). Railway und Vault sollten zwischen diesen Zeiten umgestellt werden,
   sonst gibt es nachts 401.

So sieht ein Job aus, falls einer neu angelegt werden muss. `cron.schedule` mit einem bereits
vorhandenen Namen überschreibt den Job:

```sql
select cron.schedule('cleanup-expired-stories', '0 3 * * *', $cmd$select net.http_delete(
  url := 'https://web-production-1bb6f.up.railway.app/stories/cleanup/expired',
  headers := jsonb_build_object('X-Cleanup-Secret', (select decrypted_secret from vault.decrypted_secrets where name = 'story_cleanup_secret')));$cmd$);
```

**Falle:** Im Job darf das Secret nicht mit einem Zeilenumbruch beginnen. Daran ist der
Story-Cleanup von Juli bis Oktober 2026 still gescheitert (pg_net: `A libcurl function was given
a bad argument`).

---

## 5. Prüfen

```powershell
venv\Scripts\python.exe scripts\secret_rotation_check.py
venv\Scripts\python.exe scripts\ws_gateway_prod_smoke.py ranked-ws-gateway-production.up.railway.app
```

- `secret_rotation_check.py` prüft lokale Werte (Länge, keine Dopplung, Gateway-`.env` gleich),
  den Backend-Login mit dem lokalen Key, S3, Vault gegen `.env` und ob ein Cron-Job noch ein
  Secret im Klartext enthält. Es schreibt nichts und gibt nur Fingerabdrücke aus.
- `ws_gateway_prod_smoke.py` prüft Gateway, `WS_INTERNAL_SECRET` und Redis. Es schreibt
  **zwei Testnachrichten** (Sebastian → Lama).
- **Cleanup-Jobs** lassen sich einmal von Hand auslösen: die `command`-Spalte aus `cron.job`
  ausführen, Antwort in `net._http_response` ansehen.
- **`ranking-end-of-day` nie von Hand auslösen**, sonst gibt es doppelte XP oder Streaks. Am
  Morgen danach `net._http_response` ansehen. Die Antworten bleiben dort nur etwa 6 Stunden.

---

## 6. Spuren suchen

Für das Zeitfenster des Einbruchs prüfen:

1. **Audit-Log** (`moderation_actions`): Admin- oder Mod-Aktionen, die nicht von uns sind? Gibt
   es Nutzer mit `role <> 'user'`, die da nicht hingehören?
   Ergebnis 2026-10: sauber. Im Zeitfenster gab es keine Einträge, der einzige Admin ist
   Sebastian.
2. **Supabase-Logs**: Datenbank-Verbindungen von fremden IPs (Logs → Postgres).
3. **GitHub**: Audit-Log und Zugriff der Railway-GitHub-App.
4. **Supabase Storage**: unerwartete neue oder geänderte Dateien in den Buckets.

Das Audit-Log erfasst nur Aktionen über die API. Mit dem DB-Passwort käme man daran vorbei,
deshalb ist Punkt 2 genauso wichtig.

---

## 7. DSGVO

Waren echte Nutzerdaten betroffen und ist ein Risiko nicht auszuschließen, muss innerhalb von
**72 Stunden** geprüft werden, ob eine Meldung an die Aufsichtsbehörde nötig ist (Art. 33). Im
Oktober 2026 war die App noch nicht live, es gab nur Testkonten.
