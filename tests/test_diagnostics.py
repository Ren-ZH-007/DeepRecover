import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from app.version import VERSION
from core.diagnostics import (DiagnosticsError, MANUAL_CASES, collect_environment,
                              write_environment_report)
from core.tasks import CancellationToken, TaskCancelled


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'private-user-device-serial'
        self.workspace.mkdir()
        (self.workspace / 'Reports').mkdir()
        self.display = {'version': '6.8.3', 'platform': 'offscreen', 'screen_count': 2,
                        'high_dpi_screens': 1, 'screen_names': ['private-screen']}

    def collect(self, **kwargs):
        with patch('core.diagnostics.AndroidAdapter', return_value=SimpleNamespace(executable=None)):
            return collect_environment(self.workspace, display=self.display, **kwargs)

    def test_collection_never_runs_commands_reads_files_or_creates_paths(self):
        source = self.root / 'source.img'; source.write_bytes(b'private source bytes')
        database = self.workspace / 'index.db'; database.write_bytes(b'private history')
        logs = self.workspace / 'Logs'; logs.mkdir(); (logs / 'app.log').write_bytes(b'private logs')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        # mac_ver reads Apple's SystemVersion.plist internally. Supply that
        # system metadata here so the assertions below concern user/source
        # content, rather than forbidding the OS version provider itself.
        with patch('core.diagnostics.platform.mac_ver', return_value=('15.0', ('', '', ''), 'arm64')), \
                patch('subprocess.run', side_effect=AssertionError('No commands')), \
                patch('pathlib.Path.open', side_effect=AssertionError('No file reads')), \
                patch('pathlib.Path.mkdir', side_effect=AssertionError('No directories')), \
                patch('builtins.open', side_effect=AssertionError('No file reads')):
            report = self.collect()
        self.assertEqual(report['kind'], 'environment_readiness')
        self.assertEqual(report['workspace']['status'], 'available')
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(set(p for p in self.root.rglob('*') if p.is_file()), set(before))
        text = json.dumps(report)
        for value in [str(self.root), self.workspace.name, 'private-screen', 'private history', 'private logs']:
            self.assertNotIn(value, text)

    def test_macos_version_provider_is_used_without_commands_or_user_file_reads(self):
        with patch('core.diagnostics.sys.platform', 'darwin'), \
                patch('core.diagnostics.platform.mac_ver', return_value=('15.3.1', ('', '', ''), 'arm64')) as mac_version, \
                patch('core.diagnostics.platform.release', side_effect=AssertionError('Wrong version provider')), \
                patch('subprocess.run', side_effect=AssertionError('No commands')), \
                patch('pathlib.Path.open', side_effect=AssertionError('No user file reads')), \
                patch('builtins.open', side_effect=AssertionError('No user file reads')):
            report = self.collect()
        mac_version.assert_called_once_with()
        self.assertEqual(report['system']['platform'], 'darwin')
        self.assertEqual(report['system']['os_version'], '15.3.1')
        self.assertEqual(report['manual_acceptance']['status'], 'not_run')

    def test_missing_work_directory_stays_missing_and_is_not_claimed_available(self):
        missing = self.root / 'not-created'
        report = collect_environment(missing)
        self.assertFalse(missing.exists())
        self.assertEqual(report['workspace'], {'status': 'unavailable', 'free_bytes': None, 'total_bytes': None})

    def test_metadata_errors_are_redacted_and_unknown_observations_are_explicit(self):
        with patch('core.diagnostics.shutil.disk_usage', side_effect=PermissionError('/private-user/secret-volume')), \
                patch('core.diagnostics.platform.release', side_effect=OSError('host private-host')), \
                patch('core.diagnostics.platform.machine', side_effect=OSError('private machine')), \
                patch('core.diagnostics.sys.platform', 'linux'):
            report = self.collect()
        self.assertEqual(report['workspace']['status'], 'unavailable')
        self.assertEqual(report['system']['os_version'], 'unknown')
        self.assertEqual(report['system']['architecture'], 'unknown')
        self.assertNotIn('private-', json.dumps(report))

    def test_private_version_suffixes_and_untrusted_display_strings_are_removed(self):
        self.display.update(version='6.8.3 /Users/private-user', platform='/private-user/plugin',
                            screen_count=2, high_dpi_screens=8, extra='private serial')
        with patch('core.diagnostics.platform.release', return_value='15.3.1-private-user'), \
                patch('core.diagnostics.platform.machine', return_value='Private-Computer'), \
                patch('core.diagnostics.sys.platform', 'linux'):
            report = self.collect()
        self.assertEqual(report['system']['os_version'], '15.3.1')
        self.assertEqual(report['system']['architecture'], 'unknown')
        self.assertEqual(report['qt'], {'version': 'unknown', 'platform': 'unknown',
                                       'screen_count': None, 'high_dpi_screens': None})
        self.assertNotIn('private', json.dumps(report).lower())

    def test_architecture_aliases_are_normalized(self):
        for source, expected in [('AMD64', 'x86_64'), ('aarch64', 'arm64'), ('i686', 'x86')]:
            with self.subTest(source=source), patch('core.diagnostics.platform.machine', return_value=source):
                self.assertEqual(self.collect()['system']['architecture'], expected)

    def test_capacity_is_observed_without_a_writable_or_space_pass_claim(self):
        usage = SimpleNamespace(total=1000, free=0)
        with patch('core.diagnostics.shutil.disk_usage', return_value=usage):
            report = self.collect()
        self.assertEqual(report['workspace'], {'status': 'available', 'free_bytes': 0, 'total_bytes': 1000})
        self.assertNotIn('writable', report['workspace'])
        self.assertEqual(report['manual_acceptance']['status'], 'not_run')
        self.assertTrue(all(case['status'] == 'not_run' for case in report['manual_acceptance']['cases']))
        self.assertEqual([case['id'] for case in report['manual_acceptance']['cases']], [case[0] for case in MANUAL_CASES])

    def test_adb_is_only_a_checked_locator_not_an_executed_device_probe(self):
        tool = self.root / 'private-adb-tool'; tool.write_bytes(b'not executed')
        with patch('core.diagnostics.AndroidAdapter', return_value=SimpleNamespace(executable=str(tool))), \
                patch('core.diagnostics.os.access', return_value=True), \
                patch('subprocess.run', side_effect=AssertionError('No device queries')):
            report = collect_environment(self.workspace)
        self.assertEqual(report['adb'], {'located': True})
        self.assertNotIn(str(tool), json.dumps(report))
        with patch('core.diagnostics.AndroidAdapter', return_value=SimpleNamespace(executable=str(self.root / 'missing'))):
            self.assertEqual(collect_environment(self.workspace)['adb'], {'located': False})

    def test_cancellation_before_collection_touches_no_metadata(self):
        token = CancellationToken(); token.cancel()
        with patch('core.diagnostics.platform.release', side_effect=AssertionError('Not reached')):
            with self.assertRaises(TaskCancelled): self.collect(token=token)

    def test_cancellation_between_metadata_checks_does_not_locate_adb(self):
        token = CancellationToken()
        def capacity(_):
            token.cancel()
            return SimpleNamespace(total=1000, free=500)
        with patch('core.diagnostics.shutil.disk_usage', side_effect=capacity), \
                patch('core.diagnostics.AndroidAdapter') as adapter:
            with self.assertRaises(TaskCancelled): collect_environment(self.workspace, token=token)
        adapter.assert_not_called()

    def test_saving_closed_schema_removes_injected_paths_and_cannot_mark_m5_passed(self):
        report = self.collect()
        report.update(username='private-user', path='/Users/private-user', device_serial='private-device')
        report['system']['machine_name'] = 'private-machine'
        report['manual_acceptance'] = {'status': 'passed', 'cases': [{'id': 'M5-01', 'status': 'passed'}]}
        report['app_version'] = '/private-user/version'
        target = write_environment_report(self.workspace, report)
        self.assertEqual(target.parent, self.workspace / 'Reports')
        self.assertRegex(target.name, r'^environment-[0-9a-f]{32}\.json$')
        saved = json.loads(target.read_text())
        self.assertEqual(saved['app_version'], VERSION)
        self.assertEqual(saved['manual_acceptance']['status'], 'not_run')
        self.assertTrue(all(case['status'] == 'not_run' for case in saved['manual_acceptance']['cases']))
        self.assertNotIn('private', target.read_text())
        second = write_environment_report(self.workspace, report)
        self.assertNotEqual(target, second)
        self.assertEqual(target.read_bytes(), second.read_bytes())

    def test_existing_name_is_never_overwritten_or_removed(self):
        identity = UUID(int=10)
        target = self.workspace / 'Reports' / f'environment-{identity.hex}.json'
        target.write_bytes(b'original report')
        with patch('core.diagnostics.uuid4', return_value=identity):
            with self.assertRaises(DiagnosticsError): write_environment_report(self.workspace, self.collect())
        self.assertEqual(target.read_bytes(), b'original report')

    def test_missing_reports_or_workspace_are_not_created(self):
        missing = self.root / 'missing'
        with self.assertRaises(DiagnosticsError): write_environment_report(missing, self.collect())
        self.assertFalse(missing.exists())
        (self.workspace / 'Reports').rmdir()
        with self.assertRaises(DiagnosticsError): write_environment_report(self.workspace, self.collect())
        self.assertFalse((self.workspace / 'Reports').exists())

    def test_report_directory_links_and_junction_style_redirection_are_refused(self):
        report = self.collect(); directory = self.workspace / 'Reports'
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return self.root if path == directory else original_resolve(path, *args, **kwargs)
        with patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda path: path == directory):
            with self.assertRaises(DiagnosticsError): write_environment_report(self.workspace, report)
        with patch.object(Path, 'resolve', autospec=True, side_effect=resolve):
            with self.assertRaises(DiagnosticsError): write_environment_report(self.workspace, report)
        self.assertEqual(list(directory.iterdir()), [])

    def test_directory_replacement_before_file_creation_is_refused(self):
        report = self.collect()
        with patch('core.diagnostics._same_directories', side_effect=OSError('private directory moved')):
            with self.assertRaises(DiagnosticsError) as error:
                write_environment_report(self.workspace, report)
        self.assertNotIn('private', str(error.exception))
        self.assertEqual(list((self.workspace / 'Reports').iterdir()), [])

    def test_shared_or_nonregular_open_file_is_rejected_before_writing(self):
        report = self.collect()
        real_fstat = os.fstat
        for mode, links in [(stat.S_IFREG, 2), (stat.S_IFIFO, 1)]:
            def fstat(fd):
                info = real_fstat(fd)
                if stat.S_ISDIR(info.st_mode): return info
                return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino,
                                       st_mode=mode, st_nlink=links)
            with self.subTest(mode=mode, links=links), patch('core.diagnostics.os.fstat', side_effect=fstat):
                with self.assertRaises(DiagnosticsError): write_environment_report(self.workspace, report)
            self.assertEqual(list((self.workspace / 'Reports').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
