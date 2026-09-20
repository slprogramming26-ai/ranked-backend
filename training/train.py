"""Lytir — der Trainingsloop.

Nimmt die vier Tensoren aus dataset.py, trainiert LytirNet darauf und
speichert die Epoche mit der besten Val-Loss.

Solange die Daten aus simulate.py kommen, lernt das Netz nur die
Lehrer-Regel von dort nach. Ein gutes Ergebnis heisst: die Kette laeuft.
Mehr nicht.
"""

import copy
from pathlib import Path

import torch
import torch.nn as nn

from app.lytir.features import FEATURE_VERSION
from training.dataset import Dataset, build_dataset, load_rows_from_file
from training.model import LytirNet



SEED = 42
N_EPOCHS = 1000
LERNRATE = 0.001

DATEN = Path(__file__).parent / "data" / "sim.jsonl"

# Wie sim.jsonl: aus den Seeds jederzeit neu erzeugbar, daher in data/
# (gitignored). Ein Modell aus erfundenen Daten gehoert nicht ins Repo.
MODELL = Path(__file__).parent / "data" / "lytir.pt"



def baseline_loss(ds: Dataset, loss_fn: nn.Module) -> float:
    """Val-Loss eines "Modells", das stur immer den Durchschnitt tippt.

    Die Messlatte: bei weichen Labels geht die BCE-Loss nie auf 0, eine Zahl
    wie 0.60 sagt allein also nichts. Erst der Abstand zu diesem Wert zeigt,
    ob das Netz etwas gelernt hat.

    Der Durchschnitt kommt aus y_TRAIN, nicht aus y_val — die Baseline darf
    nur wissen, was auch das Modell weiss.
    """
    p = ds.y_train.mean()
    konstant = torch.full_like(ds.y_val, torch.logit(p).item())
    return loss_fn(konstant, ds.y_val).item()


def main() -> None:
    # VOR dem Modell: die Startgewichte sind zufaellig. Mit festem Seed ist
    # jeder Lauf identisch, und zwei Laeufe lassen sich ueberhaupt vergleichen.
    torch.manual_seed(SEED)

    ds = build_dataset(load_rows_from_file(DATEN))

    model = LytirNet()
    loss_fn = nn.BCEWithLogitsLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=LERNRATE)

    baseline = baseline_loss(ds, loss_fn)
    print()
    print(f"  Train {len(ds.y_train)} Zeilen, Val {len(ds.y_val)} Zeilen")
    print(f"  Baseline (immer Durchschnitt tippen): Val-Loss {baseline:.4f}")
    print()

    beste_val_loss = float("inf")
    beste_epoche = -1
    bester_zustand = None



    for epoch in range(N_EPOCHS):
        # --- Let's train! ---
        model.train()
        logits = model(ds.X_train)
        loss = loss_fn(logits, ds.y_train)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # --- Test time! ---
        model.eval()
        with torch.inference_mode():
            val_logits = model(ds.X_val)
            val_loss = loss_fn(val_logits, ds.y_val)

                # --- Die beste Epoche merken ---
        if val_loss.item() < beste_val_loss:
            beste_val_loss = val_loss.item()
            beste_epoche = epoch
            bester_zustand = copy.deepcopy(model.state_dict())


        # --- Print out what's happenin' ---
        if epoch % 50 == 0 or epoch == N_EPOCHS - 1:
            print(f"  Epoche {epoch:4d}   Train {loss.item():.4f}   Val {val_loss.item():.4f}")


    # --- Don't forget to save save save ---
    MODELL.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": bester_zustand,
            "feature_version": FEATURE_VERSION,
            "epoche": beste_epoche,
            "val_loss": beste_val_loss,
            "baseline": baseline,
        },
        MODELL,
    )

    print()
    print(f"  Beste Epoche {beste_epoche}: Val-Loss {beste_val_loss:.4f} "
          f"(Baseline {baseline:.4f})")
    print(f"  gespeichert: {MODELL}")
    print()




if __name__ == "__main__":
    main()
