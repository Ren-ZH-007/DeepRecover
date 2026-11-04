import hashlib
import os
from pathlib import Path
import stat
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.computer import io


class ComputerIoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / '中文 image #.img'
        self.payload = bytes(range(256)) * 32
        self.path.write_bytes(self.payload)

    def test_image_reads_are_read_only_exact_and_preserve_source(self):
        original_open = os.open
        flags = []
        def open_file(path, mode, *args, **kwargs):
            flags.append(mode)
            return original_open(path, mode, *args, **kwargs)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch.object(io.os, 'open', side_effect=open_file):
            with io.DiskReader(self.path) as reader:
                self.assertEqual(reader.read_at(7, 513), self.payload[7:520])
                self.assertEqual(reader.check(), reader.identity)
                self.assertEqual(reader.identity['kind'], 'image')
                self.assertEqual(reader.size, len(self.payload))
        self.assertEqual(flags, [os.O_RDONLY | getattr(os, 'O_BINARY', 0)])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_cursor_reads_match_seekable_file_and_do_not_change_read_at_position(self):
        with io.DiskReader(self.path) as reader:
            reader.seek(10)
            self.assertEqual(reader.read(5), self.payload[10:15])
            self.assertEqual(reader.read_at(100, 3), self.payload[100:103])
            self.assertEqual(reader.tell(), 15)
            reader.seek(-2, os.SEEK_END)
            self.assertEqual(reader.read(20), self.payload[-2:])
            self.assertEqual(reader.read(1), b'')
            reader.seek(-2, os.SEEK_CUR)
            self.assertEqual(reader.read(), self.payload[-2:])

    def test_ranges_reject_negative_boolean_and_out_of_bounds_values(self):
        with io.DiskReader(self.path) as reader:
            for offset, length in [(-1, 1), (0, -1), (True, 1), (0, True), (reader.size, 1), (reader.size + 1, 0)]:
                with self.subTest(offset=offset, length=length), self.assertRaises(OSError):
                    reader.read_at(offset, length)
            for offset, whence in [(-1, os.SEEK_SET), (1, 99), (reader.size + 1, os.SEEK_SET)]:
                with self.assertRaises(OSError):
                    reader.seek(offset, whence)

    def test_large_single_read_is_rejected_before_access(self):
        with self.path.open('r+b') as stream:
            stream.truncate(io.MAX_READ + 1)
        with io.DiskReader(self.path) as reader:
            with self.assertRaises(OSError):
                reader.read_at(0, io.MAX_READ + 1)

    def test_short_read_never_returns_partial_source_bytes(self):
        with io.DiskReader(self.path) as reader:
            if hasattr(io.os, 'pread'):
                with patch.object(io.os, 'pread', return_value=b'x'), self.assertRaises(OSError):
                    reader.read_at(0, 2)
            else:
                with patch.object(io.os, 'read', return_value=b'x'), self.assertRaises(OSError):
                    reader.read_at(0, 2)

    def test_expected_image_identity_refuses_later_source_change(self):
        with io.DiskReader(self.path) as reader:
            expected = reader.check()
        info = self.path.stat()
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        with io.DiskReader(self.path) as reader:
            with self.assertRaises(io.DiskError):
                reader.check(expected)

    def test_live_path_replacement_is_detected_without_relying_on_windows_rename(self):
        with io.DiskReader(self.path) as reader:
            info = self.path.stat()
            values = {key: getattr(info, key) for key in ('st_mode', 'st_dev', 'st_ino', 'st_size',
                'st_mtime_ns', 'st_ctime_ns', 'st_birthtime_ns') if hasattr(info, key)}
            values['st_ino'] += 1
            with patch.object(io.os, 'stat', return_value=SimpleNamespace(**values)), self.assertRaises(io.DiskError):
                reader.check()

    def test_directories_closed_readers_and_unknown_kinds_are_rejected(self):
        with self.assertRaises(io.DiskError):
            with io.DiskReader(self.root):
                pass
        with self.assertRaises(io.DiskError):
            io.DiskReader(self.path, 'unknown')
        reader = io.DiskReader(self.path)
        with reader:
            pass
        with self.assertRaises(OSError):
            reader.read_at(0, 1)
        with self.assertRaises(io.DiskError):
            reader.check()

    def test_raw_devices_rejected_on_linux_before_open(self):
        with patch.object(io.sys, 'platform', 'linux'), patch.object(io.os, 'open') as open_file:
            with self.assertRaises(io.DiskError):
                io.DiskReader('/dev/sda', 'device')
            open_file.assert_not_called()

    def test_device_whitelist_does_not_allow_arbitrary_device_paths(self):
        for platform, rejected in [('win32', [r'\\.\GLOBALROOT\Device\Memory', r'\\server\share', r'\\.\PhysicalDrive0\extra']),
                                   ('darwin', ['/dev/mem', '/dev/disk0/extra', '/tmp/disk0'])]:
            with patch.object(io.sys, 'platform', platform):
                for path in rejected:
                    with self.subTest(path=path), self.assertRaises(io.DiskError):
                        io.normalize_device(path)

    def test_macos_raw_reads_are_sector_aligned_and_return_requested_slice(self):
        payload = self.payload
        fake_info = SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=123)
        reads = []
        def pread(descriptor, length, offset):
            reads.append((descriptor, length, offset))
            return payload[offset:offset + length]
        with patch.object(io.sys, 'platform', 'darwin'), patch.object(io.os, 'open', return_value=77) as open_file, \
                patch.object(io, 'normalize_device', return_value='/dev/disk999'), \
                patch.object(io.os, 'fstat', return_value=fake_info), patch.object(io.os, 'stat', return_value=fake_info), \
                patch.object(io, '_mac_geometry', return_value=(len(payload), 512)), \
                patch.object(io.os, 'pread', side_effect=pread, create=True), patch.object(io.os, 'close') as close:
            with io.DiskReader('/dev/disk999', 'device') as reader:
                self.assertEqual(reader.read_at(510, 4), payload[510:514])
                self.assertEqual(reader.read_at(len(payload) - 2, 2), payload[-2:])
            open_file.assert_called_once_with('/dev/disk999', os.O_RDONLY | getattr(os, 'O_BINARY', 0))
            close.assert_called_once_with(77)
        self.assertEqual(reads, [(77, 1024, 0), (77, 512, len(payload) - 512)])

    def test_windows_backend_uses_read_access_and_only_query_controls(self):
        api = Mock()
        api.open.return_value = 55
        geometry = bytearray(32)
        struct.pack_into('<I', geometry, 20, 4096)
        struct.pack_into('<Q', geometry, 24, 16384)
        def query(handle, code, *args, **kwargs):
            return {0x700A0: bytes(geometry), 0x7405C: struct.pack('<Q', 8192),
                    0x2D1080: struct.pack('<III', 7, 3, 1)}[code]
        api.ioctl.side_effect = query
        with patch.object(io, '_WinAPI', return_value=api):
            backend = io._WindowsBackend(r'\\.\C:')
            self.assertEqual(backend.identity['size'], 8192)
            self.assertEqual(backend.identity['sector_size'], 4096)
            backend.close()
        api.open.assert_called_once_with(r'\\.\C:', read=True)
        self.assertEqual({call.args[1] for call in api.ioctl.call_args_list}, {0x700A0, 0x7405C, 0x2D1080})
        api.close.assert_called_once_with(55)

    def test_windows_volume_extent_query_includes_all_disks_and_closes_on_errors(self):
        api = Mock(); api.open.return_value = 99
        payload = bytearray(56)
        struct.pack_into('<I', payload, 0, 2)
        struct.pack_into('<I', payload, 8, 1)
        struct.pack_into('<I', payload, 32, 4)
        api.ioctl.return_value = bytes(payload)
        with patch.object(io, '_WinAPI', return_value=api):
            self.assertEqual(io.windows_volume_disk_numbers(r'\\.\D:'), {1, 4})
            api.ioctl.return_value = b'bad'
            with self.assertRaises(io.DiskError):
                io.windows_volume_disk_numbers(r'\\.\D:')
        self.assertEqual(api.open.call_args_list[0].kwargs, {'read': False})
        self.assertEqual(api.close.call_count, 2)

    def test_macos_geometry_change_is_rejected(self):
        info = SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=9)
        with patch.object(io.sys, 'platform', 'darwin'), patch.object(io.os, 'open', return_value=66), \
                patch.object(io, 'normalize_device', return_value='/dev/disk999'), \
                patch.object(io.os, 'fstat', return_value=info), patch.object(io.os, 'stat', return_value=info), \
                patch.object(io, '_mac_geometry', return_value=(8192, 512)) as geometry, \
                patch.object(io.os, 'close'):
            with io.DiskReader('/dev/disk999', 'device') as reader:
                geometry.return_value = (16384, 512)
                with self.assertRaises(io.DiskError):
                    reader.check()
