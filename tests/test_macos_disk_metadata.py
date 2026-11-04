"""Probe boundaries with simulated metadata; these are not native acceptance."""
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from core.computer import devices
from core.computer.io import DiskError

SPEC = importlib.util.spec_from_file_location('verify_macos_metadata',
    Path(__file__).resolve().parents[1] / 'scripts/verify_macos_disk_metadata.py')
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class MacOSMetadataProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work_dir = Path(self.temp.name).resolve()
        self.root_info = {'DeviceIdentifier': 'disk3s1s1', 'APFSVolumeDisk': 'disk3s1',
            'APFSSnapshot': True, 'FilesystemType': 'apfs',
            'APFSPhysicalStores': [{'DeviceIdentifier': 'disk0s2'}],
            'VolumeUUID': 'PRIVATE-UUID', 'SerialNumber': 'PRIVATE-SERIAL'}
        self.work_info = {'DeviceIdentifier': 'disk3s5', 'APFSContainerReference': 'disk3'}
        self.apfs = {'Containers': [{'ContainerReference': 'disk3',
            'PhysicalStores': [{'DeviceIdentifier': 'disk0s2'}],
            'Volumes': [{'DeviceIdentifier': 'disk3s1'}, {'DeviceIdentifier': 'disk3s5'}]}]}
        self.commands = []

    def run_metadata_command(self, arguments):
        self.commands.append(arguments)
        if arguments == ['/usr/sbin/diskutil', 'apfs', 'list', '-plist']:
            return plistlib.dumps(self.apfs)
        if arguments[:3] == ['/usr/sbin/diskutil', 'info', '-plist']:
            if arguments[3] in ('/', '/dev/disk3s1s1'):
                return plistlib.dumps(self.root_info)
            if arguments[3] == '/dev/disk3s5':
                return plistlib.dumps(self.work_info)
        if arguments == ['/bin/df', '-P', str(self.work_dir)]:
            return b'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk3s5 100 10 90 10% /private\n'
        if arguments == ['/bin/df', '-P', '/']:
            return b'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk3s1s1 100 10 90 10% /\n'
        raise AssertionError('Unexpected command: ' + repr(arguments))

    def simulated_probe(self, **patches):
        with ExitStack() as stack:
            stack.enter_context(patch.object(verify.sys, 'platform', 'darwin'))
            stack.enter_context(patch.object(devices, '_run', side_effect=self.run_metadata_command))
            raw = stack.enter_context(patch('core.computer.io.DiskReader',
                side_effect=AssertionError('Metadata probe opened a raw source')))
            for name, replacement in patches.items():
                stack.enter_context(patch.object(devices, name, **replacement))
            result = verify.verify_metadata(self.work_dir)
            raw.assert_not_called()
            return result

    def test_non_macos_fails_before_any_system_command_or_raw_reader(self):
        with patch.object(verify.sys, 'platform', 'linux'), patch.object(devices, '_run') as run, \
                patch('core.computer.io.DiskReader') as raw:
            with self.assertRaisesRegex(RuntimeError, 'requires macOS'):
                verify.verify_metadata()
            run.assert_not_called(); raw.assert_not_called()

    def test_simulated_probe_queries_only_metadata_and_redacts_private_values(self):
        result = self.simulated_probe()
        self.assertTrue(result['same_device_rejected'])
        self.assertEqual(result['root_physical_disk_count'], 1)
        self.assertTrue(result['root_and_work_directory_overlap'])
        self.assertFalse(result['raw_reads'])
        text = json.dumps(result)
        for private in ['PRIVATE-UUID', 'PRIVATE-SERIAL', 'disk0', 'disk3', str(self.work_dir)]:
            self.assertNotIn(private, text)
        self.assertTrue(self.commands)

    def test_info_physical_store_key_and_snapshot_metadata_reach_the_guard(self):
        self.root_info['APFSPhysicalStores'] = [{'APFSPhysicalStore': 'disk0s2'}]
        result = self.simulated_probe()
        self.assertEqual(result['schemas']['root']['physical_stores']['identifier_keys'],
                         ['APFSPhysicalStore'])
        self.assertTrue(result['schemas']['root']['snapshot'])
        self.assertTrue(result['same_device_rejected'])

    def test_work_directory_on_another_disk_does_not_replace_root_guard_check(self):
        self.work_info = {'DeviceIdentifier': 'disk9s1', 'ParentWholeDisk': 'disk9'}
        with patch.object(devices, '_mac_volume_path',
                side_effect=lambda path: '/dev/disk9s1' if path == self.work_dir else '/dev/disk3s1s1'), \
                patch.object(devices, '_mac_info', side_effect=lambda path:
                    self.work_info if path == '/dev/disk9s1' else self.root_info):
            result = self.simulated_probe()
        self.assertFalse(result['root_and_work_directory_overlap'])
        self.assertTrue(result['same_device_rejected'])

    def test_unknown_work_directory_mapping_is_not_reported_as_success(self):
        self.work_info = {'DeviceIdentifier': 'disk7s1', 'FilesystemType': 'apfs'}
        with self.assertRaises(DiskError):
            self.simulated_probe()

    def test_guard_mapping_failure_does_not_count_as_same_disk_rejection(self):
        with self.assertRaisesRegex(RuntimeError, 'could not confirm'):
            self.simulated_probe(ensure_different_device={'side_effect': DiskError('Unknown metadata')})

    def test_guard_that_allows_root_volume_causes_validation_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'unexpectedly allowed'):
            self.simulated_probe(ensure_different_device={'return_value': True})
