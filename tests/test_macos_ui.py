import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QAction, QKeyEvent, QKeySequence
    from PySide6.QtWidgets import QApplication, QLabel
    from app.main_window import MainWindow, selection_modifier
    from app.theme import font_family
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

from app.app_config import AppConfig
from core.tasks import TaskCancelled
from storage.database.repository import Repository


@unittest.skipUnless(QT_AVAILABLE, 'PySide6 not installed')
class MacosUiTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = AppConfig(Path(self.temp.name))
        self.config.initialize()
        self.repo = Repository(self.config.database)
        self.windows = []
        self.addCleanup(self.close_windows)

    def close_windows(self):
        for window in self.windows:
            window.close()

    def window(self):
        window = MainWindow(self.config, self.repo)
        self.windows.append(window)
        return window

    def send_quit_shortcut(self, window, target):
        # Headless backends may expose no standard Quit key at all. Give this
        # dispatch test a controlled binding in that case; the configuration
        # test separately verifies that production requests StandardKey.Quit.
        if not window.quit_action.shortcuts():
            window.quit_action.setShortcut(QKeySequence('Ctrl+Q', QKeySequence.SequenceFormat.PortableText))
        combination = window.quit_action.shortcuts()[0][0]
        target.activateWindow()
        self.app.processEvents()
        self.app.sendEvent(target, QKeyEvent(QEvent.Type.KeyPress, combination.key(), combination.keyboardModifiers()))
        self.app.sendEvent(target, QKeyEvent(QEvent.Type.KeyRelease, combination.key(), combination.keyboardModifiers()))
        # Synthetic release events keep their modifier mask in Qt. Reset the
        # test-only keyboard state so later row-selection tests stay isolated.
        self.app.sendEvent(target, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_unknown, Qt.KeyboardModifier.NoModifier))
        self.app.sendEvent(target, QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_unknown, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self.app.keyboardModifiers(), Qt.KeyboardModifier.NoModifier)

    def test_macos_fonts_and_modifiers_preserve_other_platform_defaults(self):
        self.assertIn('Helvetica Neue', font_family('darwin'))
        self.assertIn('PingFang SC', font_family('darwin'))
        self.assertEqual(font_family('win32'), '"Segoe UI", "Microsoft YaHei", "Noto Sans CJK SC"')
        self.assertEqual(font_family('linux'), font_family('win32'))
        self.assertEqual(selection_modifier('darwin'), 'Command / Shift')
        self.assertEqual(selection_modifier('win32'), 'Ctrl / Shift')
        self.assertEqual(selection_modifier('linux'), 'Ctrl / Shift')

    def test_macos_menu_uses_native_quit_role_and_command_hints(self):
        class RecordingAction(QAction):
            def setShortcuts(self, shortcuts):
                self.requested_shortcuts = shortcuts
                super().setShortcuts(shortcuts)

        # Select the macOS UI without treating a headless backend's bindings or
        # NativeText glyphs as evidence of the Cocoa keyboard mapping.
        with patch('app.main_window.sys.platform', 'darwin'), patch('app.main_window.QAction', RecordingAction):
            window = self.window()
        self.assertIsNotNone(window.application_menu)
        self.assertIn(window.quit_action, window.application_menu.actions())
        self.assertEqual(window.quit_action.menuRole(), QAction.MenuRole.QuitRole)
        self.assertEqual(window.quit_action.requested_shortcuts, QKeySequence.StandardKey.Quit)
        self.assertEqual(window.quit_action.shortcuts(), QKeySequence.keyBindings(QKeySequence.StandardKey.Quit))
        self.assertEqual(window.quit_action.shortcutContext(), Qt.ShortcutContext.ApplicationShortcut)
        self.assertTrue(window.quit_action.isEnabled())
        self.assertEqual(window.actions(), [window.quit_action])
        self.assertIn('Command / Shift', window.android_file_table.horizontalHeaderItem(0).text())
        self.assertIn('Command / Shift', window.result_selection_hint.text())
        self.assertIn('Finder', ' '.join(label.text() for label in window.stack.widget(2).findChildren(QLabel)))

    def test_quit_action_closes_idle_window_through_close_event(self):
        window = self.window(); window.show(); self.app.processEvents()
        with patch.object(window.manager, 'shutdown', wraps=window.manager.shutdown) as shutdown:
            window.quit_action.trigger()
            self.assertFalse(window.isVisible())
            self.assertFalse(window.timer.isActive())
            shutdown.assert_called_once()

    def test_quit_action_closes_open_export_history_with_main_window(self):
        window = self.window(); window.show()
        task = self.repo.create_task('export')
        self.repo.update_task(task, 'completed')
        window.show_export_history(task)
        dialog = window.export_dialog
        self.app.processEvents()
        self.assertTrue(dialog.isVisible())
        self.assertFalse(dialog.isModal())
        with patch.object(window.manager, 'shutdown', wraps=window.manager.shutdown) as shutdown:
            window.quit_action.trigger()
            self.app.processEvents()
            self.assertFalse(window.isVisible())
            self.assertFalse(dialog.isVisible())
            shutdown.assert_called_once()

    def test_quit_shortcut_cancels_live_job_and_waits_before_closing(self):
        window = self.window(); window.show(); self.app.processEvents()
        task = self.repo.create_task('export')
        self.repo.update_task(task, 'completed')
        window.show_export_history(task)
        dialog = window.export_dialog
        self.app.processEvents()
        self.assertTrue(dialog.isVisible())
        started, release = Event(), Event()
        def operation(token):
            started.set()
            if not release.wait(3): raise RuntimeError('test worker was not released')
            token.checkpoint()
        try:
            window.run_task('test', operation, lambda *_: None)
            handle = window.pending[0]
            self.assertTrue(started.wait(3))
            with patch.object(window.manager, 'shutdown', wraps=window.manager.shutdown) as shutdown:
                self.send_quit_shortcut(window, dialog)
                self.assertTrue(window.isVisible())
                self.assertTrue(dialog.isVisible())
                self.assertTrue(window.timer.isActive())
                with self.assertRaises(TaskCancelled): handle.token.checkpoint()
                shutdown.assert_not_called()
                release.set()
                with self.assertRaises(TaskCancelled): handle.future.result(3)
                window.poll()
                self.assertEqual(self.repo.task(handle.id)['status'], 'cancelled')
                self.send_quit_shortcut(window, dialog)
                self.assertFalse(window.isVisible())
                self.assertFalse(dialog.isVisible())
                self.assertFalse(window.timer.isActive())
                shutdown.assert_called_once()
        finally:
            release.set()
            if window.pending:
                try: window.pending[0].future.result(3)
                except TaskCancelled: pass
                window.poll()
