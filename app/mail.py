

# Vorerst wird nichts verschickt: die Mail landet nur im Log.
# Zum Launch kommt hier der Aufruf an den Mail-Dienst rein (HTTP-POST + API-Key).
# Die Aufrufer (register, resend, Passwort vergessen) bleiben dann unverändert.


def send_mail(to: str, subject: str, text: str):
    print(f"[mail] an {to} | {subject}\n{text}", flush=True)
