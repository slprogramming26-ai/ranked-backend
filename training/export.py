"""Lytir — vom gespeicherten Modell zur ONNX-Datei.

train.py speichert ein PyTorch-Modell. Der Feed (und spaeter das Frontend)
soll aber kein torch brauchen. ONNX ist das neutrale Austauschformat
dazwischen: Architektur + Gewichte in einer Datei, die ohne PyTorch laeuft.
"""

import json
from pathlib import Path


import onnxruntime as ort
import torch
import torch.nn as nn

from app.lytir.features import FEATURE_VERSION, N_FEATURES
from training.dataset import build_dataset, load_rows_from_file
from training.model import LytirNet
from training.train import DATEN, MODELL
from app.lytir.ranker import _logit



# training/data/lytir.pt -> training/data/lytir.onnx
ONNX_DATEI = MODELL.with_suffix(".onnx")
# training/data/lytir.pt -> training/data/lytir.json
JSON_DATEI = MODELL.with_suffix(".json")




def load_model(pfad):
    """lytir.pt -> fertiges LytirNet im eval-Modus, plus der ganze Umschlag."""
    checkpoint = torch.load(pfad, weights_only=True)

    # Ein Modell, das auf einem anderen Feature-Layout trainiert wurde, rechnet
    # ohne jede Fehlermeldung Unsinn. Also lieber hier laut abbrechen.
    if checkpoint["feature_version"] != FEATURE_VERSION:
        raise ValueError(
            f"Modell hat FEATURE_VERSION {checkpoint['feature_version']}, "
            f"der Code {FEATURE_VERSION} — neu trainieren."
        )

    model = LytirNet()
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    return model, checkpoint


def export_onnx(model: LytirNet, pfad: Path) -> None:
    """LytirNet -> ONNX-Datei, die ohne torch laeuft."""
    # Der Exporter schickt diesen Input einmal durchs Netz und zeichnet dabei
    # jeden Rechenschritt auf. Die Werte sind egal, nur die FORM zaehlt.
    beispiel = torch.zeros(1, N_FEATURES)

    programm = torch.onnx.export(
        model,
        (beispiel,),
        input_names=["features"],
        output_names=["logit"],
        # Achse 0 (Anzahl Posts) bleibt variabel, Achse 1 (17 Features) fest.
        # Ohne das waere die Batchgroesse 1 fest eingebrannt.
        dynamic_shapes=({0: "batch"},),
    )

    # Die Version reist IN der Datei mit. Der Ranker bekommt spaeter nur die
    # .onnx, nie die .pt — er muss selbst pruefen koennen, ob sie passt.
    programm.model.metadata_props["feature_version"] = str(FEATURE_VERSION)
    programm.save(pfad)

# float32 rechnet auf ~7 Stellen genau. onnxruntime und torch summieren in
# anderer Reihenfolge, deshalb weichen die Ergebnisse minimal ab — exakt 0
# ist nicht zu erwarten. Alles ueber dieser Grenze waere ein echter Fehler.
TOLERANZ = 1e-5


def check_onnx(pfad: Path, model: LytirNet, X: torch.Tensor) -> None:
    """Rechnet die ONNX-Datei dasselbe wie PyTorch? Bricht ab, wenn nicht."""
    session = ort.InferenceSession(str(pfad))

    # Liest man die Version wieder aus der Datei heraus? Genau so wird es
    # der Ranker spaeter tun.
    version = session.get_modelmeta().custom_metadata_map["feature_version"]
    print(f"  feature_version in der Datei: {version}")

    # Dieselben Zeilen durch beide Rechenwege. Nebenbei der Beweis, dass die
    # Batchgroesse variabel ist: exportiert wurde mit 1 Zeile, hier sind es 828.
    with torch.inference_mode():
        erwartet = model(X).numpy()
    ergebnis = session.run(None, {"features": X.numpy()})[0]

    abweichung = float(abs(ergebnis - erwartet).max())
    print(f"  {len(X)} Zeilen, groesste Abweichung: {abweichung:.2e}")

    if abweichung > TOLERANZ:
        raise ValueError(f"ONNX weicht von PyTorch ab: {abweichung}")

def check_json(pfad: Path, model: LytirNet, X: torch.Tensor) -> None:
    """Rechnet ranker.py mit der JSON-Datei dasselbe wie PyTorch?"""
    daten = json.loads(pfad.read_text(encoding="utf-8"))
    schichten = [(s["W"], s["b"]) for s in daten["schichten"]]

    with torch.inference_mode():
        erwartet = model(X).tolist()

    # X.tolist() macht aus dem Tensor (828, 17) eine Liste von 828 Listen —
    # genau die Form, die der Ranker im Feed auch bekommt.
    ergebnis = [_logit(schichten, x) for x in X.tolist()]

    abweichung = max(abs(e - r) for e, r in zip(erwartet, ergebnis))
    print(f"  {len(X)} Zeilen, groesste Abweichung: {abweichung:.2e}")

    if abweichung > TOLERANZ:
        raise ValueError(f"JSON-Ranker weicht von PyTorch ab: {abweichung}")






def export_json(model: LytirNet, pfad: Path, checkpoint: dict) -> None:
    """LytirNet -> nackte Zahlen als JSON, damit ranker.py ohne torch rechnen kann."""
    schichten = []
    for modul in model.net:
        # model.net enthaelt Linear, ReLU, Linear, ReLU, Linear.
        # Nur die Linear-Schichten haben Zahlen, ReLU ist reine Rechenregel.
        if isinstance(modul, nn.Linear):
            schichten.append({
                "W": modul.weight.tolist(),   # Form (aus, ein): eine Zeile pro Neuron
                "b": modul.bias.tolist(),     # Form (aus,)
            })

    daten = {
        "feature_version": FEATURE_VERSION,
        "epoche": checkpoint["epoche"],
        "val_loss": checkpoint["val_loss"],
        "schichten": schichten,
    }
    pfad.write_text(json.dumps(daten), encoding="utf-8")




if __name__ == "__main__":
    model, checkpoint = load_model(MODELL)

    # Probe: dieselbe Val-Loss nochmal ausrechnen. Der Split ist
    # deterministisch, also sind es exakt dieselben 828 Zeilen wie im Training.
    ds = build_dataset(load_rows_from_file(DATEN))
    loss_fn = nn.BCEWithLogitsLoss()
    with torch.inference_mode():
        val_loss = loss_fn(model(ds.X_val), ds.y_val).item()

    print()
    print(f"  gespeichert:   Epoche {checkpoint['epoche']}, Val-Loss {checkpoint['val_loss']:.4f}")
    print(f"  nachgerechnet: Val-Loss {val_loss:.4f}")
    print()

    export_onnx(model, ONNX_DATEI)
    print(f"  exportiert: {ONNX_DATEI} ({ONNX_DATEI.stat().st_size} Bytes)")
    print()

    check_onnx(ONNX_DATEI, model, ds.X_val)
    print()

    export_json(model, JSON_DATEI, checkpoint)
    print(f"  exportiert: {JSON_DATEI} ({JSON_DATEI.stat().st_size} Bytes)")
    print()

    check_json(JSON_DATEI, model, ds.X_val)
    print()





