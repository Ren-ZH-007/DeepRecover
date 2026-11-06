import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.app_config import AppConfig
from core.computer.devices import DiskDevice
from core.diagnostics import MAX_ERROR_ROWS, MAX_VOLUMES, collect_environment, disk_snapshot, write_environment_report
from storage.database.repository import Repository

PRIVATE = ('/dev/disk4s1', 'Ren’s Passport', 'WD-SERIAL-123', '/Volumes/Private', 'disk4', 'disk9', '500107862016')


def devices():
    return [
        DiskDevice('/dev/disk4s1', 'Ren’s Passport', 500107862016, mountpoints=('/Volumes/Private',),
                   physical_disks=('disk4',), device_kind='partition', file_system='exFAT', internal=False, removable=True),
        DiskDevice('/dev/disk3s1', 'Macintosh HD', 494384795648, system_disk=True, physical_disks=('disk0',),
                   device_kind='apfs_volume', file_system='APFS', internal=True, roles=('System', 'WD-SERIAL-123')),
        DiskDevice('/dev/disk3s2', 'Preboot', 1, physical_disks=(), device_kind='auxiliary', file_system='APFS',
                   roles=('Preboot',)),
        DiskDevice('/dev/disk5', 'Fusion', 1, physical_disks=('disk0', 'disk9'), device_kind='apfs_container'),
    ]


class DiagnosticsDiskAndErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve() / 'work'; self.workspace.mkdir(); (self.workspace / 'Reports').mkdir()

    def collect(self, **kwargs):
        with patch('core.diagnostics.AndroidAdapter', return_value=SimpleNamespace(executable=None)):
            return collect_environment(self.workspace, display=None, **kwargs)

    def test_disk_mapping_keeps_only_closed_non_identifying_fields(self):
        report = self.collect(disks=disk_snapshot(devices()))
        disks = report['disks']
        self.assertEqual(disks['status'], 'detected')
        self.assertEqual(disks['physical_disk_count'], 3)
        by_kind = {v['kind']: v for v in disks['volumes']}
        self.assertEqual(by_kind['partition'], {'kind': 'partition', 'file_system': 'exFAT', 'roles': [], 'internal': False,
                                                'removable': True, 'system_disk': False, 'physical_mapping': 'single'})
        self.assertEqual(by_kind['apfs_volume']['roles'], ['System'])
        self.assertTrue(by_kind['apfs_volume']['system_disk'])
        self.assertEqual(by_kind['auxiliary']['physical_mapping'], 'unmapped')
        self.assertEqual(by_kind['apfs_container']['physical_mapping'], 'multiple')
        self.assertEqual(by_kind['apfs_container']['file_system'], 'unknown')
        text = json.dumps(report, ensure_ascii=False)
        for value in PRIVATE:
            self.assertNotIn(value, text)

    def test_injected_disk_fields_and_values_are_dropped(self):
        forged = {'physical_disk_count': 999, 'serial': 'WD-SERIAL-123', 'volumes': [
            {'kind': '/dev/disk4', 'file_system': 'Ren’s Passport', 'roles': ['System', '/Volumes/Private', 7],
             'internal': 'yes', 'removable': 1, 'system_disk': True, 'physical_disks': 'disk4', 'path': '/dev/disk4s1'},
            'not a dict'] + [{'kind': 'partition', 'physical_disks': 1}] * (MAX_VOLUMES + 5)}
        disks = self.collect(disks=forged)['disks']
        self.assertIsNone(disks['physical_disk_count'])
        self.assertEqual(len(disks['volumes']), MAX_VOLUMES - 1)
        odd = next(v for v in disks['volumes'] if v['kind'] == 'unknown')
        self.assertEqual(odd, {'kind': 'unknown', 'file_system': 'unknown', 'roles': ['System'], 'internal': None,
                               'removable': None, 'system_disk': True, 'physical_mapping': 'unknown'})
        text = json.dumps(disks, ensure_ascii=False)
        for value in PRIVATE:
            self.assertNotIn(value, text)

    def test_missing_disk_detection_and_errors_are_explicit(self):
        report = self.collect()
        self.assertEqual(report['disks'], {'status': 'not_detected', 'physical_disk_count': None, 'volumes': []})
        self.assertEqual(report['errors'], {'status': 'not_collected', 'tasks': [], 'exports': []})
        self.assertEqual(report['schema_version'], 2)
        self.assertEqual(disk_snapshot(None), None)

    def test_error_summary_counts_types_without_messages_or_ids(self):
        config = AppConfig(self.workspace / 'data'); config.initialize(); repo = Repository(config.database)
        for kind, status, code in [('computer_scan', 'failed', 'DiskError'), ('computer_scan', 'failed', 'DiskError'),
                                   ('signature_scan', 'cancelled', None), ('export', 'failed', 'ExportError'),
                                   ('signature_scan', 'completed', None), ('android_listing', 'failed', 'AdbError')]:
            repo.update_task(repo.create_task(kind), status, code)
        summary = repo.error_summary()
        report = self.collect(errors=summary)['errors']
        self.assertEqual(report['status'], 'collected')
        self.assertEqual(report['tasks'][0], {'kind': 'computer_scan', 'status': 'failed', 'error': 'DiskError', 'count': 2})
        self.assertEqual(sorted((r['kind'], r['status'], r['error']) for r in report['tasks']),
                         [('android_listing', 'failed', 'AdbError'), ('computer_scan', 'failed', 'DiskError'),
                          ('export', 'failed', 'ExportError'), ('signature_scan', 'cancelled', 'none')])
        self.assertEqual(report['exports'], [])
        text = json.dumps(report)
        for task in repo.tasks():
            self.assertNotIn(task['id'], text)

    def test_forged_error_rows_are_reduced_and_bounded(self):
        rows = [{'kind': 'export', 'status': 'failed', 'error': 'PermissionError: /Users/ren/secret', 'count': 3},
                {'kind': '/Users/ren', 'status': 'completed', 'error': 'OSError', 'count': 1},
                {'kind': 'export', 'status': 'failed', 'error': 'OSError', 'count': -4},
                {'kind': 'export', 'status': 'failed', 'error': 'OSError', 'count': True},
                'junk']
        errors = self.collect(errors={'tasks': rows, 'exports': [{'status': 'failed', 'error': 'X' * 65, 'count': 2}]})['errors']
        self.assertEqual(errors['tasks'], [{'kind': 'export', 'status': 'failed', 'error': 'unknown', 'count': 3},
                                           {'kind': 'unknown', 'status': 'unknown', 'error': 'OSError', 'count': 1}])
        self.assertEqual(errors['exports'], [{'status': 'failed', 'error': 'unknown', 'count': 2}])
        self.assertNotIn('/Users/ren', json.dumps(errors))
        many = [{'kind': 'export', 'status': 'failed', 'error': f'E{i}', 'count': 1 + (i == 7)} for i in range(MAX_ERROR_ROWS + 10)]
        bounded = self.collect(errors={'tasks': many, 'exports': []})['errors']['tasks']
        self.assertEqual(len(bounded), MAX_ERROR_ROWS)
        self.assertEqual(bounded[0]['error'], 'E7')

    def test_collection_with_disks_and_errors_still_reads_no_files_and_runs_no_commands(self):
        snapshot, errors = disk_snapshot(devices()), {'tasks': [], 'exports': []}
        with patch('subprocess.run', side_effect=AssertionError('No commands')), \
                patch('pathlib.Path.open', side_effect=AssertionError('No file reads')), \
                patch('builtins.open', side_effect=AssertionError('No file reads')), \
                patch('core.diagnostics.platform.mac_ver', return_value=('15.0', ('', '', ''), 'arm64')):
            report = self.collect(disks=snapshot, errors=errors)
        self.assertEqual(report['disks']['status'], 'detected')

    def test_every_state_is_unchanged_by_saving(self):
        # The packaged smoke check requires the saved report to equal the shown one.
        for disks, errors in ((None, None), (disk_snapshot(devices()), None), (None, {'tasks': [], 'exports': []}),
                              (disk_snapshot([]), {'tasks': [{'kind': 'export', 'status': 'failed', 'error': None, 'count': 1}], 'exports': []})):
            report = self.collect(disks=disks, errors=errors)
            saved = json.loads(write_environment_report(self.workspace, report).read_text(encoding='utf-8'))
            self.assertEqual(saved, report)

    def test_forged_status_cannot_turn_missing_sections_into_observations(self):
        report = self.collect(disks={'status': 'not_detected', 'volumes': [{'kind': 'partition', 'physical_disks': 1}]},
                              errors={'status': 'not_collected', 'tasks': [{'kind': 'export', 'status': 'failed', 'error': 'OSError', 'count': 1}]})
        self.assertEqual(report['disks'], {'status': 'not_detected', 'physical_disk_count': None, 'volumes': []})
        self.assertEqual(report['errors'], {'status': 'not_collected', 'tasks': [], 'exports': []})

    def test_saved_report_rebuilds_disk_and_error_sections(self):
        report = self.collect(disks=disk_snapshot(devices()), errors={'tasks': [{'kind': 'export', 'status': 'failed', 'error': 'OSError', 'count': 2}], 'exports': []})
        expected_disks, expected_errors = json.loads(json.dumps(report['disks'])), json.loads(json.dumps(report['errors']))
        clean = json.loads(write_environment_report(self.workspace, report).read_text(encoding='utf-8'))
        self.assertEqual(clean['disks'], expected_disks)
        self.assertEqual(clean['errors'], expected_errors)
        report['disks']['volumes'].append({'kind': 'partition', 'label': 'Ren’s Passport', 'physical_disks': 1})
        report['disks']['serial'] = 'WD-SERIAL-123'
        report['errors']['tasks'].append({'kind': 'export', 'status': 'failed', 'error': 'OSError', 'count': 1, 'path': '/Volumes/Private'})
        saved = write_environment_report(self.workspace, report).read_text(encoding='utf-8')
        for value in PRIVATE:
            self.assertNotIn(value, saved)
        saved = json.loads(saved)
        injected = {'kind': 'partition', 'file_system': 'unknown', 'roles': [], 'internal': None,
                    'removable': None, 'system_disk': None, 'physical_mapping': 'single'}
        self.assertEqual(saved['disks']['volumes'], sorted(expected_disks['volumes'] + [injected],
                                                           key=lambda v: json.dumps(v, sort_keys=True)))
        self.assertEqual(saved['disks']['physical_disk_count'], expected_disks['physical_disk_count'])
        self.assertEqual(saved['errors']['tasks'], [{'kind': 'export', 'status': 'failed', 'error': 'OSError', 'count': 3}])


if __name__ == '__main__':
    unittest.main()
