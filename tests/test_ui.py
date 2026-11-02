import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
try:
    from PySide6.QtWidgets import QApplication
    from app.main_window import MainWindow
    QT_AVAILABLE=True
except ImportError:
    QT_AVAILABLE=False
from app.app_config import AppConfig
from storage.database.repository import Repository

@unittest.skipUnless(QT_AVAILABLE,'PySide6 not installed')
class UiTests(unittest.TestCase):
    def test_start_navigation_and_close(self):
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            config=AppConfig(Path(directory)); config.initialize()
            window=MainWindow(config,Repository(config.database)); window.show()
            app.processEvents()
            for index in range(7):
                window.nav.setCurrentRow(index); app.processEvents()
                self.assertEqual(window.stack.currentIndex(),index)
            window.close(); app.processEvents()
            self.assertFalse(window.isVisible())
