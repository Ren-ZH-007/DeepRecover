import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.computer import devices
from core.computer.io import DiskError


class ComputerDeviceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.apfs = {'Containers': [{'ContainerReference': 'disk3',
            'PhysicalStores': [{'DeviceIdentifier': 'disk0s2'}],
            'Volumes': [{'DeviceIdentifier': 'disk3s1'}, {'DeviceIdentifier': 'disk3s5'}]}]}

    def test_windows_inventory_lists_physical_disks_and_volumes_without_shell(self):
        payload = {'disks': [{'Number': 0, 'FriendlyName': '内部盘', 'Size': 100000, 'IsSystem': True},
                             {'Number': 2, 'FriendlyName': 'USB', 'Size': 50000}],
                   'partitions': [{'DiskNumber': 0, 'DriveLetter': 'C', 'Size': 80000,
                                   'AccessPaths': ['C:\\']},
                                  {'DiskNumber': 2, 'DriveLetter': 'E', 'Size': 50000,
                                   'AccessPaths': ['E:\\']}]}
        def runner(arguments, **kwargs):
            self.assertEqual(arguments[:4], ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command'])
            self.assertNotIn('shell', kwargs)
            self.assertNotIn('Set-Disk', arguments[4])
            if arguments[4] == devices.WINDOWS_INVENTORY:
                self.assertEqual(kwargs['timeout'], 20)
                result = payload
            else:
                self.assertEqual(arguments[4], devices.WINDOWS_VOLUME_INVENTORY)
                self.assertEqual(kwargs['timeout'], 5)
                result = {'volumes': []}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(result, ensure_ascii=False).encode('utf-8'), b'')
        with patch.object(devices.sys, 'platform', 'win32'), patch.object(devices.subprocess, 'run', side_effect=runner):
            found = {item.path: item for item in devices.list_devices()}
        self.assertEqual(set(found), {r'\\.\PhysicalDrive0', r'\\.\PhysicalDrive2', r'\\.\C:', r'\\.\E:'})
        self.assertTrue(found[r'\\.\C:'].system_disk)
        self.assertFalse(found[r'\\.\E:'].system_disk)
        self.assertEqual(found[r'\\.\E:'].mountpoints, ('E:\\',))

    def test_invalid_inventory_rows_cannot_create_untrusted_device_paths(self):
        payload = {'disks': [None, {'Number': '../bad', 'Size': 100}, {'Number': True, 'Size': 100},
                             {'Number': 1, 'Size': -1}],
                   'partitions': [None, {'DriveLetter': 'C:;bad', 'Size': 100}]}
        with patch.object(devices, '_windows_inventory', return_value=payload):
            self.assertEqual(devices._windows_devices(), [])

    def test_macos_inventory_preserves_partitions_and_marks_synthesized_system_volumes(self):
        inventory = {'AllDisksAndPartitions': [
            {'DeviceIdentifier': 'disk0', 'Size': 100000, 'Content': 'GUID_partition_scheme',
             'Partitions': [{'DeviceIdentifier': 'disk0s2', 'Size': 90000, 'Content': 'Apple_APFS'}]},
            {'DeviceIdentifier': 'disk2', 'Size': 50000, 'Content': 'FDisk_partition_scheme',
             'Partitions': [{'DeviceIdentifier': 'disk2s1', 'Size': 49000, 'VolumeName': 'USB 中文', 'MountPoint': '/Volumes/USB 中文'}]},
            {'DeviceIdentifier': 'disk3', 'Size': 90000, 'Content': 'Apple_APFS',
             'APFSVolumes': [{'DeviceIdentifier': 'disk3s1', 'Size': 40000, 'VolumeName': 'Data'}]}]}
        with patch.object(devices.sys, 'platform', 'darwin'), patch.object(devices, '_plist', return_value=inventory) as plist, \
                patch.object(devices, '_mac_apfs', return_value=self.apfs), \
                patch.object(devices, '_mac_info', return_value={'DeviceIdentifier': 'disk3s1', 'APFSContainerReference': 'disk3'}):
            found = {item.path: item for item in devices.list_devices()}
        plist.assert_called_once_with(['/usr/sbin/diskutil', 'list', '-plist'])
        self.assertTrue(found['/dev/disk3s1'].system_disk)
        self.assertTrue(found['/dev/disk0'].system_disk)
        self.assertFalse(found['/dev/disk2'].system_disk)
        self.assertEqual(found['/dev/disk2s1'].mountpoints, ('/Volumes/USB 中文',))

    def test_apfs_mapping_uses_every_fusion_physical_store(self):
        apfs = {'Containers': [{'ContainerReference': 'disk8', 'PhysicalStores': [
            {'DeviceIdentifier': 'disk0s2'}, {'DeviceIdentifier': 'disk1s2'}],
            'Volumes': [{'DeviceIdentifier': 'disk8s1'}]}]}
        ids = devices._mac_physical_ids({'DeviceIdentifier': 'disk8s1', 'APFSContainerReference': 'disk8'}, apfs)
        self.assertEqual(ids, {'disk0', 'disk1'})

    def test_diskutil_info_physical_store_key_maps_volume_and_snapshot(self):
        for identifier in ('disk3s1', 'disk3s1s1'):
            info = {'DeviceIdentifier': identifier, 'APFSContainerReference': 'disk3',
                    'APFSVolumeDisk': 'disk3s1',
                    'APFSPhysicalStores': [{'APFSPhysicalStore': 'disk0s2'}]}
            with self.subTest(identifier=identifier):
                self.assertEqual(devices._mac_physical_ids(info, self.apfs), {'disk0'})
                self.assertEqual(devices._mac_physical_ids(info, {}), {'disk0'})

    def test_snapshot_volume_reference_maps_only_through_apfs_inventory(self):
        info = {'DeviceIdentifier': 'disk3s1s1', 'APFSVolumeDisk': 'disk3s1',
                'APFSSnapshot': True, 'ParentWholeDisk': 'disk3'}
        self.assertEqual(devices._mac_physical_ids(info, self.apfs), {'disk0'})

    def test_snapshot_volume_reference_without_mapping_is_rejected(self):
        info = {'DeviceIdentifier': 'disk3s1s1', 'APFSVolumeDisk': 'disk3s1',
                'APFSSnapshot': True, 'ParentWholeDisk': 'disk3'}
        with self.assertRaises(DiskError):
            devices._mac_physical_ids(info, {})

    def test_direct_apfs_stores_and_inventory_must_agree(self):
        info = {'DeviceIdentifier': 'disk3s1', 'APFSContainerReference': 'disk3',
                'APFSPhysicalStores': [{'DeviceIdentifier': 'disk2s2'}]}
        with self.assertRaises(DiskError):
            devices._mac_physical_ids(info, self.apfs)

    def test_conflicting_direct_container_reference_is_rejected_without_inventory(self):
        for additions in [
                {'DeviceIdentifier': 'disk3s1'},
                {'DeviceIdentifier': 'disk3s1s1', 'APFSVolumeDisk': 'disk3s1'},
                {'APFSVolumeDisk': 'disk3s1'},
                {'DeviceIdentifier': 'disk3s1', 'VirtualOrPhysical': 'Physical'}]:
            info = {'APFSContainerReference': 'disk8',
                    'APFSPhysicalStores': [{'APFSPhysicalStore': 'disk0s2'}], **additions}
            with self.subTest(info=info), self.assertRaises(DiskError):
                devices._mac_physical_ids(info, {})

    def test_explicit_physical_apfs_store_can_reference_synthesized_container(self):
        info = {'DeviceIdentifier': 'disk0s2', 'VirtualOrPhysical': 'Physical',
                'APFSContainerReference': 'disk3',
                'APFSPhysicalStores': [{'APFSPhysicalStore': 'disk0s2'}]}
        self.assertEqual(devices._mac_physical_ids(info, {}), {'disk0'})
        self.assertEqual(devices._mac_physical_ids(info, self.apfs), {'disk0'})
        del info['APFSPhysicalStores']
        self.assertEqual(devices._mac_physical_ids(info, self.apfs), {'disk0'})

    def test_conflicting_or_malformed_physical_store_descriptors_are_rejected(self):
        for stores in [None, [], [{}], [{'OtherIdentifier': 'disk0s2'}],
                       [{'APFSPhysicalStore': None}], [{'APFSPhysicalStore': 'disk0;command'}],
                       [{'DeviceIdentifier': 'disk0s2', 'APFSPhysicalStore': 'disk2s2'}],
                       [{'DeviceIdentifier': 'disk0s2', 'APFSPhysicalStore': 'disk0s3'}],
                       [{'DeviceIdentifier': 'disk0s2', 'APFSPhysicalStore': None}]]:
            info = {'DeviceIdentifier': 'disk3s1', 'APFSContainerReference': 'disk3',
                    'APFSPhysicalStores': stores}
            with self.subTest(stores=stores), self.assertRaises(DiskError):
                devices._mac_physical_ids(info, self.apfs)

    def test_equivalent_apfs_store_descriptor_aliases_are_accepted(self):
        info = {'DeviceIdentifier': 'disk3s1', 'APFSContainerReference': 'disk3',
                'APFSPhysicalStores': [{'DeviceIdentifier': '/dev/rdisk0s2',
                                        'APFSPhysicalStore': 'disk0s2'}]}
        self.assertEqual(devices._mac_physical_ids(info, self.apfs), {'disk0'})

    def test_conflicting_snapshot_or_container_references_are_rejected(self):
        for additions in [{'APFSContainerReference': 'disk8'}, {'APFSVolumeDisk': 'disk8s1'},
                          {'APFSVolumeDisk': {'DeviceIdentifier': 'disk3s1'}},
                          {'APFSVolumeDisk': 'not-a-device'}]:
            info = {'DeviceIdentifier': 'disk3s1s1', 'APFSVolumeDisk': 'disk3s1', **additions}
            with self.subTest(additions=additions), self.assertRaises(DiskError):
                devices._mac_physical_ids(info, self.apfs)

    def test_conflicting_apfs_inventory_matches_cannot_choose_one_store(self):
        apfs = {'Containers': self.apfs['Containers'] + [{'ContainerReference': 'disk8',
            'PhysicalStores': [{'DeviceIdentifier': 'disk2s2'}],
            'Volumes': [{'DeviceIdentifier': 'disk3s1'}]}]}
        with self.assertRaises(DiskError):
            devices._mac_physical_ids({'DeviceIdentifier': 'disk3s1'}, apfs)

    def test_info_fusion_stores_protect_every_member_disk(self):
        info = {'DeviceIdentifier': 'disk8s1', 'APFSContainerReference': 'disk8',
                'APFSPhysicalStores': [{'APFSPhysicalStore': 'disk0s2'},
                                       {'APFSPhysicalStore': 'disk1s2'}]}
        self.assertEqual(devices._mac_physical_ids(info, {}), {'disk0', 'disk1'})
        for member in ('disk0', 'disk1'):
            with self.subTest(member=member), patch.object(devices.sys, 'platform', 'darwin'), \
                    patch.object(devices, 'normalize_device', return_value='/dev/disk8s1'), \
                    patch.object(devices, '_mac_apfs', return_value={}), \
                    patch.object(devices, '_mac_info', side_effect=[info, {'DeviceIdentifier': member}]), \
                    patch.object(devices, '_mac_volume_path', return_value='/dev/' + member):
                with self.assertRaisesRegex(DiskError, '另一块物理磁盘'):
                    devices.ensure_different_device('/dev/disk8s1', self.root / 'not-created')
        self.assertFalse((self.root / 'not-created').exists())

    def test_real_shape_source_same_internal_data_disk_rejected_before_directory_creation(self):
        source = {'DeviceIdentifier': 'disk3s1', 'APFSContainerReference': 'disk3',
                  'APFSPhysicalStores': [{'APFSPhysicalStore': 'disk0s2'}]}
        target = {'DeviceIdentifier': 'disk3s5', 'APFSContainerReference': 'disk3',
                  'APFSPhysicalStores': [{'APFSPhysicalStore': 'disk0s2'}]}
        with patch.object(devices.sys, 'platform', 'darwin'), \
                patch.object(devices, 'normalize_device', return_value='/dev/disk3s1'), \
                patch.object(devices, '_mac_apfs', return_value=self.apfs), \
                patch.object(devices, '_mac_info', side_effect=[source, target]), \
                patch.object(devices, '_mac_volume_path', return_value='/dev/disk3s5'):
            with self.assertRaisesRegex(DiskError, '另一块物理磁盘'):
                devices.ensure_different_device('/dev/disk3s1', self.root / 'not-created')
        self.assertFalse((self.root / 'not-created').exists())

    def test_real_shape_source_and_external_workdisk_are_accepted(self):
        source = {'DeviceIdentifier': 'disk3s1', 'APFSContainerReference': 'disk3',
                  'APFSPhysicalStores': [{'APFSPhysicalStore': 'disk0s2'}]}
        target = {'DeviceIdentifier': 'disk4s1', 'ParentWholeDisk': 'disk4', 'VirtualOrPhysical': 'Physical'}
        with patch.object(devices.sys, 'platform', 'darwin'), \
                patch.object(devices, 'normalize_device', return_value='/dev/disk3s1'), \
                patch.object(devices, '_mac_apfs', return_value=self.apfs), \
                patch.object(devices, '_mac_info', side_effect=[source, target]), \
                patch.object(devices, '_mac_volume_path', return_value='/dev/disk4s1'):
            self.assertTrue(devices.ensure_different_device('/dev/disk3s1', self.root / 'not-created'))
        self.assertFalse((self.root / 'not-created').exists())

    def test_unknown_apfs_virtual_mapping_never_falls_back_to_synthesized_number(self):
        for info in [{'DeviceIdentifier': 'disk3s1', 'APFSContainerReference': 'disk3'},
                     {'DeviceIdentifier': 'disk3s1', 'Content': 'Apple_APFS'},
                     {'DeviceIdentifier': 'disk4', 'VirtualOrPhysical': 'Virtual'},
                     {'DeviceIdentifier': 'disk3', 'APFSPhysicalStores': []}]:
            with self.subTest(info=info), self.assertRaises(DiskError):
                devices._mac_physical_ids(info, {})

    def test_macos_guard_rejects_destination_apfs_volume_on_source_physical_disk(self):
        info = {'/dev/disk0': {'DeviceIdentifier': 'disk0', 'VirtualOrPhysical': 'Physical'},
                '/dev/disk3s1': {'DeviceIdentifier': 'disk3s1', 'APFSContainerReference': 'disk3'}}
        destination = self.root / 'must-not-create' / 'nested'
        with patch.object(devices.sys, 'platform', 'darwin'), \
                patch.object(devices, 'normalize_device', return_value='/dev/disk0'), \
                patch.object(devices, '_mac_apfs', return_value=self.apfs), \
                patch.object(devices, '_mac_info', side_effect=lambda path: info[str(path)]), \
                patch.object(devices, '_mac_volume_path', return_value='/dev/disk3s1'):
            with self.assertRaisesRegex(DiskError, '另一块物理磁盘'):
                devices.ensure_different_device('/dev/disk0', destination)
        self.assertFalse(destination.parent.exists())

    def test_macos_guard_accepts_confirmed_different_physical_disk(self):
        def info(path):
            identifier = str(path).removeprefix('/dev/')
            return {'DeviceIdentifier': identifier, 'VirtualOrPhysical': 'Physical'}
        with patch.object(devices.sys, 'platform', 'darwin'), \
                patch.object(devices, 'normalize_device', return_value='/dev/disk2'), \
                patch.object(devices, '_mac_apfs', return_value={}), \
                patch.object(devices, '_mac_info', side_effect=info), \
                patch.object(devices, '_mac_volume_path', return_value='/dev/disk4s1'):
            self.assertTrue(devices.ensure_different_device('/dev/disk2', self.root / 'future'))

    def test_guard_rejects_unresolved_target_instead_of_assuming_other_disk(self):
        with patch.object(devices.sys, 'platform', 'darwin'), \
                patch.object(devices, 'normalize_device', return_value='/dev/disk0'), \
                patch.object(devices, '_mac_apfs', return_value={}), \
                patch.object(devices, '_mac_info', return_value={'DeviceIdentifier': 'disk0'}), \
                patch.object(devices, '_mac_volume_path', side_effect=DiskError('unknown filesystem')):
            with self.assertRaises(DiskError):
                devices.ensure_different_device('/dev/disk0', self.root)

    def test_windows_guard_checks_all_spanned_destination_extents(self):
        with patch.object(devices.sys, 'platform', 'win32'), \
                patch.object(devices, 'normalize_device', return_value=r'\\.\PhysicalDrive2'), \
                patch.object(devices, '_windows_volume_path', return_value='volume-guid') as locate, \
                patch.object(devices, 'windows_volume_disk_numbers', return_value={2, 4}) as extents:
            with self.assertRaises(DiskError):
                devices.ensure_different_device(r'\\.\PhysicalDrive2', self.root / 'future')
        locate.assert_called_once_with(self.root)
        extents.assert_called_once_with('volume-guid')

    def test_windows_volume_source_and_directory_mount_are_mapped_physically(self):
        extents = Mock(side_effect=[{0}, {3}])
        with patch.object(devices.sys, 'platform', 'win32'), \
                patch.object(devices, 'normalize_device', return_value=r'\\.\C:'), \
                patch.object(devices, '_windows_volume_path', return_value='volume-mounted-under-C'), \
                patch.object(devices, 'windows_volume_disk_numbers', extents), \
                patch.object(devices, '_windows_inventory', return_value={'disks': [
                    {'Number': 0, 'BusType': 17}, {'Number': 3, 'BusType': 7}]}):
            self.assertTrue(devices.ensure_different_device(r'\\.\C:', self.root))
        self.assertEqual([call.args[0] for call in extents.call_args_list], [r'\\.\C:', 'volume-mounted-under-C'])

    def test_windows_virtual_storage_cannot_prove_physical_separation(self):
        for bus in (14, 15, 16, 8, 9, 0, 'Spaces', 'Virtual', None):
            with self.subTest(bus=bus), patch.object(devices.sys, 'platform', 'win32'), \
                    patch.object(devices, 'normalize_device', return_value=r'\\.\PhysicalDrive0'), \
                    patch.object(devices, '_windows_volume_path', return_value='volume-guid'), \
                    patch.object(devices, 'windows_volume_disk_numbers', return_value={3}), \
                    patch.object(devices, '_windows_inventory', return_value={'disks': [
                        {'Number': 0, 'BusType': 17}, {'Number': 3, 'BusType': bus}]}):
                with self.assertRaises(DiskError):
                    devices.ensure_different_device(r'\\.\PhysicalDrive0', self.root)

    def test_windows_missing_disk_inventory_cannot_establish_separation(self):
        with patch.object(devices.sys, 'platform', 'win32'), \
                patch.object(devices, 'normalize_device', return_value=r'\\.\PhysicalDrive0'), \
                patch.object(devices, '_windows_volume_path', return_value='volume-guid'), \
                patch.object(devices, 'windows_volume_disk_numbers', return_value={3}), \
                patch.object(devices, '_windows_inventory', return_value={'disks': [{'Number': 0, 'BusType': 17}]}):
            with self.assertRaises(DiskError):
                devices.ensure_different_device(r'\\.\PhysicalDrive0', self.root)

    def test_empty_windows_extent_mapping_is_rejected(self):
        with patch.object(devices.sys, 'platform', 'win32'), \
                patch.object(devices, 'normalize_device', return_value=r'\\.\PhysicalDrive0'), \
                patch.object(devices, '_windows_volume_path', return_value='unknown-volume'), \
                patch.object(devices, 'windows_volume_disk_numbers', return_value=set()):
            with self.assertRaises(DiskError):
                devices.ensure_different_device(r'\\.\PhysicalDrive0', self.root)

    def test_macos_df_command_keeps_unicode_and_shell_characters_in_one_argument(self):
        path = self.root / '中文 # $()'
        with patch.object(devices, '_run', return_value=b'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk2s1 100 20 80 20% /Volumes/USB\n') as run:
            self.assertEqual(devices._mac_volume_path(path), '/dev/disk2s1')
        run.assert_called_once_with(['/bin/df', '-P', str(path)])
        with patch.object(devices, '_run', return_value=b'Filesystem\nserver:/share 100 20 80 20% /mount\n'), self.assertRaises(DiskError):
            devices._mac_volume_path(path)

    def test_invalid_system_metadata_is_reported_without_payload_leak(self):
        for output in [b'not-json', b'[]']:
            with patch.object(devices, '_run', return_value=output), self.assertRaises(DiskError):
                devices._windows_inventory()
        for output in [b'<?xml version="1.0"?><plist><dict>', plistlib.dumps([])]:
            with patch.object(devices, '_run', return_value=output), self.assertRaises(DiskError):
                devices._plist(['synthetic-command'])

    def test_unsupported_platform_never_runs_device_tools(self):
        with patch.object(devices.sys, 'platform', 'linux'), patch.object(devices.subprocess, 'run') as run:
            with self.assertRaises(DiskError):
                devices.list_devices()
            with self.assertRaises(DiskError):
                devices.ensure_different_device('/dev/sda', self.root)
            run.assert_not_called()
