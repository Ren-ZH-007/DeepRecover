"""Run from project root: python scripts/check_ui.py. Produces an optional QA image."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tempfile
from PySide6.QtWidgets import QApplication
from app.app_config import AppConfig
from app.main_window import MainWindow
from storage.database.repository import Repository
app=QApplication([])
with tempfile.TemporaryDirectory() as directory:
    config=AppConfig(Path(directory));config.initialize()
    window=MainWindow(config,Repository(config.database))
    window.show();app.processEvents()
    window.grab().save(str(Path(__file__).resolve().parents[1]/'docs/interface-preview.png'))
    window.close()
