"""Lytir — die Modellarchitektur.

Hier kommt das neuronale Netz rein. Absichtlich winzig: rund tausend
Parameter, ein MLP mit ein bis zwei versteckten Schichten. Das ist kein
Kompromiss, sondern die richtige Groesse — bei 17 Features und simulierten
Daten wuerde ein groesseres Netz die Beispiele auswendig lernen statt der
Regel dahinter.

WARUM LIEGT DIESE DATEI IN training/ UND NICHT IN app/lytir/?

Weil sie torch importiert. app/lytir/ laeuft im Railway-Container mit und darf
nur stdlib benutzen (Regel Nummer zwei in app/lytir/features.py). Die Trennung
geht nur in eine Richtung:

    training/  --importiert-->  app/lytir/features.py
    app/lytir/ --importiert-->  NICHTS aus training/

Im fertigen Feed existiert dieses Netz nicht als Python-Klasse. Dort liegen nur
die gelernten Zahlen, und app/lytir/ranker.py rechnet sie ohne torch aus.


WAS HIER REIN SOLL
------------------
1. Eine Klasse, die von nn.Module erbt.
2. Sie nimmt einen Vektor der Laenge N_FEATURES und gibt EINE Zahl aus:
   "wie gut passt dieser Post zu diesem User".
3. Zwischen den Schichten eine nichtlineare Funktion.
4. Eine forward()-Methode, die auf einem ganzen Batch arbeitet:
   Eingabe (batch, N_FEATURES) -> Ausgabe (batch,).

Zwei Fragen, ueber die es sich lohnt vorher nachzudenken:

  - Warum braucht es zwischen zwei nn.Linear ueberhaupt etwas Nichtlineares?
    Was waere das Netz ohne, verglichen mit der Formel in ranking_config.py?

  - Soll am Ende ein Sigmoid stehen, damit eine Wahrscheinlichkeit zwischen
    0 und 1 rauskommt? (Hier gibt es eine richtige Antwort, und sie ist nicht
    die naheliegende. Wir gehen sie durch, wenn du soweit bist.)
"""

import torch
import torch.nn as nn

# Importiert "nach unten" in den Serving-Code: die Eingabegroesse des Netzes ist
# per Konstruktion dieselbe Zahl, die features.py produziert.
from app.lytir.features import FEATURE_VERSION, N_FEATURES


# Erstelle ein model
class LytirNet(nn.Module):

  def __init__(self, hidden: tuple[int, int] = (32,16)):
    super().__init__()

    h1, h2 = hidden
    # hier definieren ich die parameter entweder über nn.Parameter() oder nn. Sequential
    self.net = nn.Sequential(
      nn.Linear(N_FEATURES, h1),
      nn.ReLU(),
      nn.Linear(h1, h2),
      nn.ReLU(),
      nn.Linear(h2, 1)
  
    )

  def forward(self, x: torch.Tensor) -> torch.Tensor:
    return self.net(x).squeeze(-1)

  @torch.no_grad()
  def predict_proba(self, x: torch.Tensor) -> torch.Tensor:
    """Logit -> Wahrscheinlichkeit 0..1. Nur zum Draufschauen.

    Fuers Ranking nicht noetig: Sigmoid ist streng monoton, die Sortierung
    nach Logit ist identisch zur Sortierung nach Wahrscheinlichkeit.
    Das Training benutzt diese Methode nie — dort geht der rohe Logit in
    BCEWithLogitsLoss.

    @torch.no_grad() schaltet die Gradientenberechnung ab: spart Speicher
    und macht unmissverstaendlich, dass hier nicht gelernt wird.
    """
    return torch.sigmoid(self(x))



if __name__ == "__main__":
    model = LytirNet()
    print(model)
    print("Feature-Version:", FEATURE_VERSION)
    print("Parameter:", sum(p.numel() for p in model.parameters()))

    x = torch.randn(4, N_FEATURES)   # 4 erfundene (User, Post)-Paare
    print()
    print(x.shape, "->", model(x).shape)
    print("Logits:            ", model(x).tolist())
    print("Wahrscheinlichkeit:", model.predict_proba(x).tolist())
   