"""File modification times from FAT32, exFAT, NTFS, iOS backups and Android."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import plistlib
import sqlite3
import struct
import subprocess
import tempfile
import unittest

from core.acquisition.android import acquire_files
from core.computer.exfat import scan_exfat
from core.computer.fat import scan_fat32
from core.computer.ntfs import scan_ntfs
from core.device.android.adapter import AndroidAdapter
from core.file_times import (display_time, exfat_modified, fat_modified, filetime_modified, unix_modified)
from core.parser.ios_backup.manifest import iter_manifest
from core.tasks import CancellationToken
from tests.exfat_fixture import ExfatImage, entry_set
from tests.fat_fixture import Fat32Fixture, short_entry
from tests.fixtures_ntfs import NTFSFixture, file_name, resident_attr

# 2024-05-01 13:45:30 in FAT/exFAT encoding.
DOS_DATE = (44 << 9) | (5 << 5) | 1
DOS_TIME = (13 << 11) | (45 << 5) | 15
NOON_UTC = datetime(2024, 5, 1, 12, 0, 0)
NOON_UNIX = int(NOON_UTC.replace(tzinfo=timezone.utc).timestamp())


def filetime(moment):
    return int((moment - datetime(1601, 1, 1)) / timedelta(microseconds=1)) * 10


def manifest_blob(**fields):
    return plistlib.dumps({'$archiver': 'NSKeyedArchiver', '$version': 100000, '$top': {'root': plistlib.UID(1)},
                           '$objects': ['$null', {'$class': plistlib.UID(2), **fields}, {'$classname': 'MBFile'}]},
                          fmt=plistlib.FMT_BINARY)


class ConversionTests(unittest.TestCase):
    def test_fat_local_wall_clock(self):
        self.assertEqual(fat_modified(DOS_DATE, DOS_TIME), {'modified_at': '2024-05-01T13:45:30', 'modified_basis': 'local'})
        self.assertEqual(fat_modified(0, 0), {})
        self.assertEqual(fat_modified((44 << 9) | (13 << 5) | 1, 0), {})   # month 13
        self.assertEqual(fat_modified((44 << 9) | (2 << 5) | 30, 0), {})   # 30 February
        self.assertEqual(fat_modified(DOS_DATE, (24 << 11)), {})            # hour 24

    def test_exfat_offsets_convert_to_utc_and_missing_offset_stays_local(self):
        stamp = (DOS_DATE << 16) | DOS_TIME
        self.assertEqual(exfat_modified(stamp, 0, 0x80 | 32), {'modified_at': '2024-05-01T05:45:30', 'modified_basis': 'utc'})
        self.assertEqual(exfat_modified(stamp, 0, 0x80 | (128 - 20)), {'modified_at': '2024-05-01T18:45:30', 'modified_basis': 'utc'})
        self.assertEqual(exfat_modified(stamp, 150, 0), {'modified_at': '2024-05-01T13:45:31', 'modified_basis': 'local'})
        self.assertEqual(exfat_modified(stamp, 250, 32), {'modified_at': '2024-05-01T13:45:30', 'modified_basis': 'local'})
        self.assertEqual(exfat_modified(0, 0, 0x80), {})

    def test_filetime_and_unix_are_utc_and_implausible_values_are_dropped(self):
        self.assertEqual(filetime_modified(filetime(NOON_UTC)), {'modified_at': '2024-05-01T12:00:00', 'modified_basis': 'utc'})
        self.assertEqual(unix_modified(NOON_UNIX), {'modified_at': '2024-05-01T12:00:00', 'modified_basis': 'utc'})
        self.assertEqual(unix_modified(NOON_UNIX + 0.75), {'modified_at': '2024-05-01T12:00:00', 'modified_basis': 'utc'})
        for value in (0, -1, 1, filetime(datetime(1979, 12, 31)), filetime(datetime(2100, 1, 1)), 2**63 - 1, None, '1'):
            self.assertEqual(filetime_modified(value), {}, value)
        for value in (0, -5, 10**13, float('nan'), True, None, '1714564800'):
            self.assertEqual(unix_modified(value), {}, value)

    def test_display(self):
        eight = timezone(timedelta(hours=8))
        self.assertEqual(display_time('2024-05-01T12:00:00', 'utc', tz=eight), '2024-05-01 20:00')
        self.assertEqual(display_time('2024-05-01T13:45:30', 'local', tz=eight), '2024-05-01 13:45（本地，无时区）')
        for value, basis in ((None, 'utc'), ('nonsense', 'utc'), ('2024-05-01T12:00:00+08:00', 'utc'), ('2024-05-01T12:00:00', 'x')):
            self.assertEqual(display_time(value, basis, tz=eight), '')


class FileSystemTimeTests(unittest.TestCase):
    def test_fat32_deleted_entry_time(self):
        fixture = Fat32Fixture()
        fixture.entries([short_entry(size=4, write_date=DOS_DATE, write_time=DOS_TIME), short_entry(b'OLD     TXT', size=4)])
        fixture.payload(3, b'data')
        rows = list(scan_fat32(fixture.reader(), fixture.partition, CancellationToken()))
        self.assertEqual((rows[0]['modified_at'], rows[0]['modified_basis']), ('2024-05-01T13:45:30', 'local'))
        self.assertNotIn('modified_at', rows[1])

    def test_exfat_deleted_entry_time(self):
        stamp = (DOS_DATE << 16) | DOS_TIME
        image = ExfatImage().finish_root([entry_set('a.png', modified=stamp, modified_offset=0x80 | 32),
                                          entry_set('b.png', first=7, modified=stamp)])
        rows = {r['name']: r for r in scan_exfat(image.reader(), image.partition, CancellationToken())}
        self.assertEqual((rows['a.png']['modified_at'], rows['a.png']['modified_basis']), ('2024-05-01T05:45:30', 'utc'))
        self.assertEqual(rows['b.png']['modified_basis'], 'local')

    def test_ntfs_prefers_standard_information_then_file_name(self):
        fixture = NTFSFixture()
        info = bytearray(48); struct.pack_into('<QQ', info, 0, filetime(datetime(2020, 1, 1)), filetime(NOON_UTC))
        fixture.write_record(16, [resident_attr(0x10, bytes(info)),
                                  resident_attr(0x30, file_name('si.txt', modified=filetime(datetime(2019, 1, 1)))),
                                  resident_attr(0x80, b'one')])
        fixture.write_record(17, [resident_attr(0x30, file_name('fn.txt', modified=filetime(datetime(2021, 6, 1, 8)))),
                                  resident_attr(0x80, b'two')])
        fixture.write_record(18, [resident_attr(0x30, file_name('none.txt')), resident_attr(0x80, b'three')])
        rows = {r['name']: r for r in scan_ntfs(fixture.reader(), fixture.partition, CancellationToken())}
        self.assertEqual((rows['si.txt']['modified_at'], rows['si.txt']['modified_basis']), ('2024-05-01T12:00:00', 'utc'))
        self.assertEqual(rows['fn.txt']['modified_at'], '2021-06-01T08:00:00')
        self.assertNotIn('modified_at', rows['none.txt'])


class AcquisitionTimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_ios_manifest_last_modified_and_unsafe_blobs(self):
        backup = self.root / 'backup'; backup.mkdir()
        (backup / 'Manifest.plist').write_bytes(plistlib.dumps({'IsEncrypted': False}))
        xml = plistlib.dumps({'$objects': [{'LastModified': NOON_UNIX}]})
        rows = [(f'{1:040x}', manifest_blob(LastModified=NOON_UNIX, Size=3)), (f'{2:040x}', manifest_blob(Size=3)),
                (f'{3:040x}', xml), (f'{4:040x}', b'bplist00' + b'\0' * 40), (f'{5:040x}', None),
                (f'{6:040x}', manifest_blob(LastModified='soon')), (f'{7:040x}', b'bplist00' + b'x' * (64 * 1024))]
        with closing(sqlite3.connect(backup / 'Manifest.db')) as db, db:
            db.execute('CREATE TABLE Files(fileID TEXT PRIMARY KEY,domain TEXT,relativePath TEXT,flags INTEGER,file BLOB) WITHOUT ROWID')
            db.executemany('INSERT INTO Files VALUES(?,?,?,1,?)',
                           ((file_id, 'HomeDomain', f'Documents/{index}.jpg', blob) for index, (file_id, blob) in enumerate(rows)))
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in backup.iterdir()}
        items = list(iter_manifest(backup))
        self.assertEqual((items[0].modified_at, items[0].modified_basis), ('2024-05-01T12:00:00', 'utc'))
        self.assertTrue(all(item.modified_at is None for item in items[1:]))
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in backup.iterdir()})

    def test_android_stat_time_after_validated_pull(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command); output = ''
            if command[1:] == ['devices', '-l']:
                output = 'SERIAL device\n'
            elif command[3] == 'shell' and command[4].startswith('readlink '):
                output = '/sdcard/DCIM/a.jpg\n'
            elif command[3] == 'shell' and command[4].startswith('stat -c %Y '):
                output = f'{NOON_UNIX}\n' if 'a.jpg' in command[4] else 'stat: no such file\n'
            elif command[3] == 'pull':
                Path(command[-1]).write_bytes(b'data')
            return subprocess.CompletedProcess(command, 0, output, '')
        adapter = AndroidAdapter('adb', runner)
        items = list(acquire_files(adapter, 'SERIAL', ['/sdcard/DCIM/a.jpg', '/sdcard/DCIM/b.jpg'], self.root / 'out', CancellationToken()))
        self.assertEqual((items[0].modified_at, items[0].modified_basis), ('2024-05-01T12:00:00', 'utc'))
        self.assertIsNone(items[1].modified_at)
        order = [c[3] if c[3] != 'shell' else c[4].split()[0] for c in calls if len(c) > 3]
        self.assertLess(order.index('pull'), order.index('stat'))

    def test_android_stat_failure_never_fails_acquisition(self):
        def runner(command, **kwargs):
            if command[1:] == ['devices', '-l']:
                return subprocess.CompletedProcess(command, 0, 'SERIAL device\n', '')
            if command[3] == 'shell' and command[4].startswith('stat '):
                return subprocess.CompletedProcess(command, 1, '', 'denied')
            if command[3] == 'shell' and command[4].startswith('readlink '):
                return subprocess.CompletedProcess(command, 0, '/sdcard/a.jpg\n', '')
            if command[3] == 'pull':
                Path(command[-1]).write_bytes(b'data')
            return subprocess.CompletedProcess(command, 0, '', '')
        items = list(acquire_files(AndroidAdapter('adb', runner), 'SERIAL', ['/sdcard/a.jpg'], self.root / 'out', CancellationToken()))
        self.assertEqual(len(items), 1); self.assertIsNone(items[0].modified_at)



class DateFilterTests(unittest.TestCase):
    def setUp(self):
        from datetime import date
        from zoneinfo import ZoneInfo
        from app.app_config import AppConfig
        from storage.database.repository import Repository
        from storage.models.candidate import Candidate
        self.date, self.zone = date, ZoneInfo
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        config = AppConfig(Path(self.temp.name)); config.initialize(); self.repo = Repository(config.database)
        self.task = self.repo.create_task('computer_scan')
        rows = [('utc-0430-1659', '2024-04-30T16:59:59', 'utc'), ('utc-0430-1700', '2024-04-30T17:00:00', 'utc'),
                ('utc-0501-1559', '2024-05-01T15:59:59', 'utc'), ('utc-0501-1600', '2024-05-01T16:00:00', 'utc'),
                ('local-0501-0000', '2024-05-01T00:00:00', 'local'), ('local-0501-2359', '2024-05-01T23:59:59', 'local'),
                ('local-0502-0000', '2024-05-02T00:00:00', 'local'), ('undated', None, None), ('odd-basis', '2024-05-01T12:00:00', 'x')]
        self.repo.add_candidates(self.task, [Candidate('computer_deleted', n, 'jpg', 1, modified_at=m, modified_basis=b) for n, m, b in rows])

    def names(self, first, last, tz, **filters):
        page = [c['name'] for c in self.repo.candidate_page(self.task, dates=(first, last), tz=tz, **filters)]
        self.assertEqual(self.repo.candidate_count(self.task, dates=(first, last), tz=tz, **filters), len(page))
        self.assertEqual(len(self.repo.candidate_ids(self.task, dates=(first, last), tz=tz, **filters)), len(page))
        return page

    def test_utc_records_use_viewer_midnights_and_local_records_use_calendar_days(self):
        day = self.date(2024, 5, 1)
        shanghai = self.zone('Asia/Shanghai')   # UTC+8: 1 May runs 30 Apr 16:00Z – 1 May 16:00Z
        self.assertEqual(self.names(day, day, shanghai),
                         ['utc-0430-1659', 'utc-0430-1700', 'utc-0501-1559', 'local-0501-0000', 'local-0501-2359'])
        london = self.zone('Europe/London')     # BST, UTC+1
        self.assertEqual(self.names(day, day, london), ['utc-0501-1559', 'utc-0501-1600', 'local-0501-0000', 'local-0501-2359'])

    def test_range_is_inclusive_and_undated_or_unknown_basis_never_match(self):
        tz = timezone.utc
        self.assertEqual(self.names(self.date(2024, 4, 30), self.date(2024, 5, 2), tz)[-1], 'local-0502-0000')
        self.assertEqual(len(self.names(self.date(2024, 4, 30), self.date(2024, 5, 2), tz)), 7)
        self.assertEqual(self.repo.candidate_count(self.task), 9)
        self.assertEqual(self.names(self.date(2024, 5, 3), self.date(2024, 5, 9), tz), [])

    def test_date_filter_combines_with_search(self):
        day = self.date(2024, 5, 1)
        self.assertEqual(self.names(day, day, timezone.utc, query='local'), ['local-0501-0000', 'local-0501-2359'])

    def test_invalid_ranges_are_rejected(self):
        from datetime import datetime as dt
        d = self.date
        for dates in ((d(2024, 5, 2), d(2024, 5, 1)), (d(1979, 12, 31), d(2024, 1, 1)), (d(2024, 1, 1), d(2100, 1, 1)),
                      (dt(2024, 5, 1), dt(2024, 5, 2)), ('2024-05-01', '2024-05-02'), (d(2024, 5, 1),), [d(2024, 5, 1)] * 2):
            with self.assertRaises(ValueError, msg=repr(dates)):
                self.repo.candidate_count(self.task, dates=dates)

if __name__ == '__main__':
    unittest.main()
