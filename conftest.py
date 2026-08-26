"""Sorgt dafuer, dass `from app import models` in den Tests funktioniert.

pytest legt das Verzeichnis dieser Datei auf den Importpfad. Weil sie im
Projekt-Wurzelverzeichnis liegt, findet Python von dort aus das Paket `app`.
Die Datei muss sonst nichts tun — allein ihre Existenz an dieser Stelle reicht.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
