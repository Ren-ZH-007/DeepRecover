"""Presentation metadata is queried read-only and never replaces disk guards."""
from copy import deepcopy
import json
import subprocess
import unittest
from unittest.mock import patch

from core.computer import devices


class DeviceInventoryGroupTests(unittest.TestCase):
    def setUp(self):
        self.inventory = {'AllDisksAndPartitions': [
            {'DeviceIdentifier': 'disk0', 'Size': 100000, 'Internal': True,
             'Content': 'GUID_partition_scheme', 'Partitions': [
                {'DeviceIdentifier': 'disk0s2', 'Size': 90000, 'Content': 'Apple_APFS'}]},
            {'DeviceIdentifier': 'disk2', 'Size': 80000, 'Internal': False, 'Ejectable': True,
             'Content': 'GUID_partition_scheme', 'Partitions': [
                {'DeviceIdentifier': 'disk2s1', 'Size': 79000, 'Content': 'Microsoft Basic Data',
                 'FilesystemType': 'exfat', 'VolumeName': 'USB 工作盘', 'MountPoint': '/Volumes/USB 工作盘'}]},
            {'DeviceIdentifier': 'disk3', 'Size': 90000, 'Content': 'Apple_APFS', 'APFSVolumes': [
                {'DeviceIdentifier': 'disk3s1', 'Size': 90000, 'VolumeName': 'Macintosh HD'},
                {'DeviceIdentifier': 'disk3s5', 'Size': 90000, 'VolumeName': 'Macintosh HD - Data'}]}]}
        self.apfs = {'Containers': [{'ContainerReference': 'disk3', 'CapacityCeiling': 90000,
            'PhysicalStores': [{'DeviceIdentifier': 'disk0s2'}], 'Volumes': [
                {'DeviceIdentifier': 'disk3s1', 'Name': 'Macintosh HD', 'Roles': ['System']},
                {'DeviceIdentifier': 'disk3s5', 'Name': 'Macintosh HD - Data', 'Roles': ['Data']},
                {'DeviceIdentifier': 'disk3s2', 'Name': 'Preboot', 'Roles': ['Preboot']},
                {'DeviceIdentifier': 'disk3s3', 'Name': 'Recovery', 'Roles': ['Recovery']}]}]}
        self.root = {'DeviceIdentifier': 'disk3s1s1', 'APFSSnapshot': True,
                     'APFSVolumeDisk': 'disk3s1', 'APFSContainerReference': 'disk3',
                     'APFSPhysicalStores': [{'APFSPhysicalStore': 'disk0s2'}],
                     'VolumeName': 'Macintosh HD', 'MountPoint': '/', 'Internal': True,
                     'TotalSize': 90000}

    def mac_devices(self):
        with patch.object(devices, '_plist', return_value=self.inventory) as listing, \
                patch.object(devices, '_mac_apfs', return_value=self.apfs) as apfs, \
                patch.object(devices, '_mac_info', return_value=self.root) as info, \
                patch('core.computer.io.DiskReader', side_effect=AssertionError('Raw read during inventory')) as raw:
            result = {item.path: item for item in devices._mac_devices()}
        listing.assert_called_once_with(['/usr/sbin/diskutil', 'list', '-plist'])
        apfs.assert_called_once_with(); info.assert_called_once_with('/')
        raw.assert_not_called()
        return result

    def windows_devices(self, inventory):
        with patch.object(devices, '_windows_inventory', return_value=inventory), \
                patch('core.computer.io.DiskReader', side_effect=AssertionError('Raw read during inventory')) as raw:
            result = {item.path: item for item in devices._windows_devices()}
        raw.assert_not_called()
        return result

    def test_legacy_five_argument_device_construction_remains_compatible(self):
        device = devices.DiskDevice('/dev/disk7', '历史测试盘', 1024, True, ('/Volumes/Old',))
        self.assertEqual(device.physical_disks, ())
        self.assertEqual(device.device_kind, 'unknown')
        self.assertIsNone(device.internal)
        self.assertFalse(device.removable)
        self.assertEqual(device.roles, ())

    def test_windows_physical_disk_and_letter_volume_share_group_and_filesystem(self):
        inventory = {'disks': [{'Number': 0, 'Size': 100000, 'IsSystem': True, 'BusType': 17},
                               {'Number': 2, 'Size': 80000, 'BusType': 'USB'}],
                     'partitions': [{'DiskNumber': 0, 'DriveLetter': 'C', 'Size': 90000},
                                    {'DiskNumber': 2, 'DriveLetter': 'E', 'Size': 79000}],
                     'volumes': [{'DriveLetter': 'c', 'FileSystemType': 'NTFS'},
                                 {'DriveLetter': 'E', 'FileSystemType': 12, 'FileSystem': 'exFAT',
                                  'FileSystemLabel': '照片盘', 'Size': 79500}]}
        found = self.windows_devices(inventory)
        physical, usb = found[r'\\.\PhysicalDrive0'], found[r'\\.\PhysicalDrive2']
        self.assertEqual(physical.device_kind, 'physical')
        self.assertEqual(physical.physical_disks, (physical.path,))
        self.assertIsNone(physical.internal)  # NVMe alone cannot establish placement.
        self.assertTrue(usb.removable)
        self.assertEqual(found[r'\\.\E:'].physical_disks, (usb.path,))
        self.assertEqual(found[r'\\.\E:'].device_kind, 'partition')
        self.assertTrue(found[r'\\.\E:'].removable)
        self.assertEqual(found[r'\\.\E:'].label, 'E: · 照片盘')
        self.assertEqual(found[r'\\.\E:'].size, 79500)
        self.assertEqual(found[r'\\.\E:'].file_system, 'exFAT')
        self.assertEqual(found[r'\\.\C:'].file_system, 'NTFS')
        self.assertTrue(found[r'\\.\C:'].system_disk)

    def test_windows_shared_letter_keeps_all_members_instead_of_duplicating_rows(self):
        inventory = {'disks': [{'Number': 1, 'Size': 10000, 'Internal': True, 'BusType': 17},
                               {'Number': 2, 'Size': 10000, 'Internal': False, 'BusType': 7}],
                     'partitions': [{'DiskNumber': 1, 'DriveLetter': 'F', 'Size': 8000},
                                    {'DiskNumber': 2, 'DriveLetter': 'f', 'Size': 8000}],
                     'volumes': [{'DriveLetter': 'F', 'Size': 16000, 'FileSystem': 'NTFS'}]}
        found = self.windows_devices(inventory)
        volume = found[r'\\.\F:']
        self.assertEqual(len(found), 3)
        self.assertEqual(volume.physical_disks, (r'\\.\PhysicalDrive1', r'\\.\PhysicalDrive2'))
        self.assertEqual(volume.size, 16000)
        self.assertIsNone(volume.internal)

    def test_windows_virtual_or_unknown_storage_is_not_labelled_as_confirmed_physical(self):
        for bus in [None, 0, 8, 9, 14, 15, 16, 'Virtual', 'Spaces']:
            with self.subTest(bus=bus):
                found = self.windows_devices({'disks': [{'Number': 3, 'Size': 10000, 'BusType': bus}],
                    'partitions': [{'DiskNumber': 3, 'DriveLetter': 'F', 'Size': 8000}]})
                for path in (r'\\.\PhysicalDrive3', r'\\.\F:'):
                    self.assertEqual(found[path].device_kind, 'unknown')
                    self.assertEqual(found[path].physical_disks, ())

    def test_windows_missing_optional_volume_data_preserves_disk_and_volume(self):
        inventory = {'disks': [{'Number': 2, 'Size': 10000, 'BusType': 7}],
                     'partitions': [{'DiskNumber': 2, 'DriveLetter': 'E', 'Size': 8000}]}
        found = self.windows_devices(inventory)
        self.assertEqual(set(found), {r'\\.\PhysicalDrive2', r'\\.\E:'})
        self.assertEqual(found[r'\\.\E:'].file_system, '')

    def test_windows_optional_volume_error_or_timeout_cannot_suppress_required_inventory(self):
        required = {'disks': [{'Number': 2, 'Size': 10000, 'BusType': 7}], 'partitions': []}
        for error in [OSError('optional access denied'), subprocess.TimeoutExpired('powershell', 5)]:
            with self.subTest(error=type(error).__name__), \
                    patch.object(devices.subprocess, 'run', side_effect=[
                        subprocess.CompletedProcess([], 0, json.dumps(required).encode(), b''), error]) as run:
                result = devices._windows_inventory(include_volumes=True)
                self.assertEqual(result, {**required, 'volumes': []})
                self.assertEqual(run.call_args_list[1].kwargs['timeout'], 5)

    def test_windows_guard_inventory_does_not_request_optional_volume_information(self):
        required = {'disks': [{'Number': 2, 'Size': 10000, 'BusType': 7}], 'partitions': []}
        with patch.object(devices, '_run', return_value=json.dumps(required).encode()) as run:
            devices._confirmed_windows_disks({2})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], devices.WINDOWS_INVENTORY)

    def test_windows_malformed_optional_metadata_is_ignored_but_required_failure_is_not(self):
        required = {'disks': [{'Number': 2, 'Size': 10000, 'BusType': 7}], 'partitions': []}
        for optional in (b'invalid json', b'\xff', b'null'):
            with self.subTest(optional=optional), patch.object(devices, '_run',
                    side_effect=[json.dumps(required).encode(), optional]):
                self.assertEqual(devices._windows_inventory(include_volumes=True), {**required, 'volumes': []})
        with patch.object(devices, '_run', side_effect=devices.DiskError('Required inventory unavailable')) as run:
            with self.assertRaises(devices.DiskError):
                devices._windows_inventory(include_volumes=True)
            self.assertEqual(run.call_count, 1)

    def test_windows_unknown_parent_preserves_source_in_unknown_group(self):
        for number in (9, None, {'invalid': 'disk'}):
            with self.subTest(number=number):
                found = self.windows_devices({'disks': [],
                    'partitions': [{'DiskNumber': number, 'DriveLetter': 'E', 'Size': 8000}]})
                self.assertEqual(found[r'\\.\E:'].physical_disks, ())
                self.assertEqual(found[r'\\.\E:'].device_kind, 'unknown')

    def test_macos_apfs_roles_and_root_snapshot_have_explicit_physical_group(self):
        found = self.mac_devices()
        self.assertEqual(found['/dev/disk0'].device_kind, 'physical')
        self.assertEqual(found['/dev/disk3'].device_kind, 'apfs_container')
        for path, role in [('/dev/disk3s1', 'System'), ('/dev/disk3s5', 'Data')]:
            self.assertEqual(found[path].device_kind, 'apfs_volume')
            self.assertEqual(found[path].roles, (role,))
            self.assertEqual(found[path].physical_disks, ('/dev/disk0',))
            self.assertEqual(found[path].file_system, 'APFS')
            self.assertTrue(found[path].internal)
            self.assertTrue(found[path].system_disk)
        self.assertEqual(found['/dev/disk3s2'].device_kind, 'auxiliary')
        self.assertEqual(found['/dev/disk3s3'].device_kind, 'auxiliary')
        snapshot = found['/dev/disk3s1s1']
        self.assertEqual(snapshot.device_kind, 'snapshot')
        self.assertEqual(snapshot.physical_disks, ('/dev/disk0',))
        self.assertEqual(snapshot.file_system, 'APFS')
        self.assertTrue(snapshot.system_disk)
        self.assertEqual(snapshot.mountpoints, ('/',))

    def test_macos_external_partition_inherits_explicit_transport_flags(self):
        found = self.mac_devices()
        volume = found['/dev/disk2s1']
        self.assertEqual(volume.device_kind, 'partition')
        self.assertEqual(volume.physical_disks, ('/dev/disk2',))
        self.assertEqual(volume.file_system, 'exFAT')
        self.assertIs(volume.internal, False)
        self.assertTrue(volume.removable)
        self.assertFalse(volume.system_disk)

    def test_macos_volume_name_never_proves_an_auxiliary_role(self):
        for roles in ([], ['Data'], ['UnknownRole']):
            with self.subTest(roles=roles):
                self.apfs['Containers'][0]['Volumes'][1].update(Name='Preboot', Roles=roles)
                self.inventory['AllDisksAndPartitions'][2]['APFSVolumes'][1]['VolumeName'] = 'Recovery'
                volume = self.mac_devices()['/dev/disk3s5']
                self.assertEqual(volume.device_kind, 'apfs_volume')
        self.apfs['Containers'][0]['Volumes'][1].update(Roles=['Preboot'], Role=42)
        volume = self.mac_devices()['/dev/disk3s5']
        self.assertEqual(volume.device_kind, 'apfs_volume')
        self.assertEqual(volume.roles, ())

    def test_macos_mixed_data_and_auxiliary_roles_stay_visible_as_data_volume(self):
        self.apfs['Containers'][0]['Volumes'][1]['Roles'] = ['Data', 'Preboot']
        volume = self.mac_devices()['/dev/disk3s5']
        self.assertEqual(volume.roles, ('Data', 'Preboot'))
        self.assertEqual(volume.device_kind, 'apfs_volume')

    def test_macos_unmapped_volume_is_preserved_without_inventing_a_physical_parent(self):
        self.apfs = {}
        self.root = {'DeviceIdentifier': 'disk3s1', 'APFSContainerReference': 'disk3'}
        found = self.mac_devices()
        self.assertIn('/dev/disk3s1', found)
        self.assertEqual(found['/dev/disk3s1'].device_kind, 'unknown')
        self.assertEqual(found['/dev/disk3s1'].physical_disks, ())

    def test_macos_fusion_container_and_volume_keep_both_physical_members(self):
        self.inventory['AllDisksAndPartitions'].append({'DeviceIdentifier': 'disk1',
            'Size': 100000, 'Content': 'GUID_partition_scheme', 'Internal': True})
        self.apfs['Containers'].append({'ContainerReference': 'disk8', 'CapacityCeiling': 180000,
            'PhysicalStores': [{'DeviceIdentifier': 'disk0s2'}, {'DeviceIdentifier': 'disk1s2'}],
            'Volumes': [{'DeviceIdentifier': 'disk8s1', 'Name': 'Fusion Data', 'Roles': ['Data']}]})
        found = self.mac_devices()
        for path, kind in [('/dev/disk8', 'apfs_container'), ('/dev/disk8s1', 'apfs_volume')]:
            self.assertEqual(found[path].physical_disks, ('/dev/disk0', '/dev/disk1'))
            self.assertEqual(found[path].device_kind, kind)
            self.assertTrue(found[path].internal)

    def test_macos_conflicting_container_metadata_does_not_choose_one_mapping(self):
        other = deepcopy(self.apfs['Containers'][0])
        other.update(ContainerReference='disk8', PhysicalStores=[{'DeviceIdentifier': 'disk2s1'}])
        self.apfs['Containers'].append(other)
        volume = self.mac_devices()['/dev/disk3s5']
        self.assertEqual(volume.device_kind, 'unknown')
        self.assertEqual(volume.physical_disks, ())


if __name__ == '__main__':
    unittest.main()
