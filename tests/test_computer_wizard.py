"""Source-tree user flows with synthetic inventory; no real device is queried."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_computer_ui as base


def device(path, label, kind='unknown', physical=(), **properties):
    values = dict(path=path, label=label, size=1024 * 1024,
        device_kind=kind, physical_disks=physical, system_disk=False,
        mountpoints=(), file_system='', roles=(), internal=None,
        removable=False)
    values.update(properties)
    return SimpleNamespace(**values)


@unittest.skipUnless(base.QT_AVAILABLE, 'PySide6 not installed')
class ComputerWizardTests(unittest.TestCase):
    setUp = base.ComputerUiTests.setUp
    close_window = base.ComputerUiTests.close_window
    partition = base.ComputerUiTests.partition
    prepare_source = base.ComputerUiTests.prepare_source
    finish_probe = base.ComputerUiTests.finish_probe

    def test_physical_groups_keep_data_and_unknown_visible_and_hide_only_marked_auxiliary(self):
        inventory = [device('/dev/disk0', '内部磁盘', 'physical', ('/dev/disk0',)),
            device('/dev/disk3s1', 'Macintosh HD', 'apfs_volume', ('/dev/disk0',)),
            device('/dev/disk3s5', '用户数据', 'apfs_volume', ('/dev/disk0',)),
            device('/dev/disk3s2', 'Preboot', 'auxiliary', ('/dev/disk0',)),
            device('/dev/disk3s1s1', '当前系统快照', 'snapshot', ('/dev/disk0',)),
            device('/dev/disk9s1', 'Preboot')]
        self.assertTrue(self.page.set_devices(inventory))
        physical = self.page.device_item('/dev/disk0')
        self.assertIs(self.page.device_item('/dev/disk3s5').parent(), physical)
        self.assertIsNotNone(self.page.device_item('/dev/disk3s1'))
        self.assertIsNotNone(self.page.device_item('/dev/disk9s1'))
        self.assertIsNone(self.page.device_item('/dev/disk3s2'))
        self.assertIsNone(self.page.device_item('/dev/disk3s1s1'))
        self.assertIsNone(self.page.selected_device())
        self.assertFalse(self.page.use_device_button.isEnabled())
        self.page.show_auxiliary.setChecked(True)
        self.assertIsNotNone(self.page.device_item('/dev/disk3s2'))
        self.assertIsNotNone(self.page.device_item('/dev/disk3s1s1'))
        self.assertIsNone(self.page.selected_device())
        self.assertEqual(self.repo.tasks(), [])

    def test_selection_uses_source_path_instead_of_filtered_or_grouped_row_index(self):
        internal = device('/dev/disk3s1', '内部卷', 'apfs_volume', ('/dev/disk0',))
        external = device('/dev/disk2s1', '外置测试卷', 'partition', ('/dev/disk2',))
        self.page.set_devices([device('/dev/disk3s2', '隐藏辅助卷', 'auxiliary', ('/dev/disk0',)),
                               internal, external])
        self.page.device_tree.setCurrentItem(self.page.device_item(external.path))
        with patch('ui.computer.page.inspect_computer_source', return_value=[self.partition()]) as inspect:
            self.page.choose_device(); self.finish_probe()
        inspect.assert_called_once_with(external.path, 'device', self.config.data_dir)
        self.assertEqual(self.page.source_path, external.path)
        self.assertTrue(self.page.start_button.isEnabled())
        self.assertIn('步骤 3 / 3', self.page.preflight_steps.text())
        self.assertEqual(self.repo.tasks(), [])

    def test_refresh_and_filter_invalidate_preflight_and_cannot_scan_old_source(self):
        source = device('/dev/disk2s1', '外置测试卷', 'partition', ('/dev/disk2',))
        for change in ('filter', 'refresh'):
            with self.subTest(change=change):
                self.page.set_devices([source])
                self.page.device_tree.setCurrentItem(self.page.device_item(source.path))
                with patch('ui.computer.page.inspect_computer_source', return_value=[self.partition()]):
                    self.page.choose_device(); self.finish_probe()
                if change == 'filter':
                    self.page.show_auxiliary.setChecked(not self.page.show_auxiliary.isChecked())
                else:
                    self.page.set_devices([device('/dev/disk7s1', '另一个来源')])
                self.assertIsNone(self.page.source_path)
                self.assertIsNone(self.page.selected_device())
                self.assertEqual(self.page.partitions, [])
                self.assertFalse(self.page.start_button.isEnabled())
                with patch('ui.computer.page.QMessageBox.question') as question, \
                        patch('ui.computer.page.scan_computer_source') as scan:
                    self.page.start_scan()
                    question.assert_not_called(); scan.assert_not_called()
        self.assertEqual(self.repo.tasks(), [])

    def test_highlighting_another_volume_invalidates_old_preflight_before_scan(self):
        first, second = device('/dev/disk2s1', '先前来源'), device('/dev/disk4s1', '新选中来源')
        self.page.set_devices([first, second])
        self.page.device_tree.setCurrentItem(self.page.device_item(first.path))
        with patch('ui.computer.page.inspect_computer_source', return_value=[self.partition()]):
            self.page.choose_device(); self.finish_probe()
        self.assertTrue(self.page.start_button.isEnabled())
        self.page.device_tree.setCurrentItem(self.page.device_item(second.path))
        self.assertIsNone(self.page.source_path)
        self.assertFalse(self.page.start_button.isEnabled())
        self.assertIs(self.page.selected_device(), second)
        self.assertTrue(self.page.use_device_button.isEnabled())
        self.assertEqual(self.repo.tasks(), [])

    def test_cross_disk_group_is_not_a_source_and_preserves_every_member(self):
        volume = device('/dev/disk8s1', '跨盘数据卷', 'apfs_volume', ('/dev/disk0', '/dev/disk1'))
        self.page.set_devices([volume])
        item = self.page.device_item(volume.path); heading = item.parent()
        self.assertIn('/dev/disk0', heading.text(0)); self.assertIn('/dev/disk1', heading.text(0))
        self.assertEqual(heading.childCount(), 1)
        self.page.device_tree.setCurrentItem(heading)
        with patch('ui.computer.page.inspect_computer_source') as inspect:
            self.page.choose_device(); inspect.assert_not_called()
        self.assertIsNone(self.page.source_path)
        self.page.device_tree.setCurrentItem(item)
        self.assertIs(self.page.selected_device(), volume)

    def test_detection_clears_old_raw_selection_without_selecting_a_new_source(self):
        source = device('/dev/disk2s1', '旧来源')
        self.page.set_devices([source]); self.page.device_tree.setCurrentItem(self.page.device_item(source.path))
        with patch('ui.computer.page.inspect_computer_source', return_value=[self.partition()]):
            self.page.choose_device(); self.finish_probe()
        with patch('ui.computer.page.list_computer_devices', return_value=[device('/dev/disk4s1', '新来源')]), \
                patch('ui.computer.page.inspect_computer_source') as inspect:
            self.page.detect_devices(); self.finish_probe(); inspect.assert_not_called()
        self.assertIsNone(self.page.selected_device()); self.assertIsNone(self.page.source_path)
        self.assertFalse(self.page.start_button.isEnabled()); self.assertEqual(self.repo.tasks(), [])

    def test_failed_reprobe_cannot_keep_an_old_confirmed_range_enabled(self):
        source = device('/dev/disk2s1', '重新检查的来源')
        self.page.set_devices([source]); self.page.device_tree.setCurrentItem(self.page.device_item(source.path))
        with patch('ui.computer.page.inspect_computer_source', return_value=[self.partition()]):
            self.page.choose_device(); self.finish_probe()
        self.assertTrue(self.page.start_button.isEnabled())
        with patch('ui.computer.page.inspect_computer_source', side_effect=RuntimeError('设备访问权限已变化')):
            self.page.inspect_selected()
            with self.assertRaises(RuntimeError): self.page._future.result(5)
            self.window.poll(); self.app.processEvents()
        self.assertEqual(self.page.partitions, [])
        self.assertFalse(self.page.start_button.isEnabled())
        self.assertIn('设备访问权限已变化', self.page.status.text())
        self.assertIn('尚未确认', self.page.capability_label.text())
        self.assertEqual(self.repo.tasks(), [])

    def test_inventory_changes_are_blocked_while_readonly_probe_is_running(self):
        source = device('/dev/disk2s1', '正在预检的来源')
        self.page.set_devices([source]); self.page.device_tree.setCurrentItem(self.page.device_item(source.path))
        def inspect(*_):
            self.release.wait(5)
            return [self.partition('APFS')]
        with patch('ui.computer.page.inspect_computer_source', side_effect=inspect):
            self.page.choose_device()
            self.assertFalse(self.page.show_auxiliary.isEnabled())
            self.assertFalse(self.page.workspace_button.isEnabled())
            self.assertFalse(self.page.set_devices([device('/dev/disk7s1', '不能覆盖来源')]))
            self.release.set(); self.finish_probe()
        self.assertEqual(self.page.source_path, source.path)
        self.assertFalse(self.page.mode_combo.model().item(0).isEnabled())
        self.assertEqual(self.page.mode_combo.currentData(), 'signature')
        self.assertTrue(self.page.start_button.isEnabled())
        self.assertEqual(self.repo.tasks(), [])
