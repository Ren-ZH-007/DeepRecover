import hashlib
import os
from contextlib import closing
import json
import plistlib
import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from core.acquisition.android import acquire_files
from core.device.android.adapter import AndroidAdapter, AdbError
from core.parser.ios_backup.manifest import iter_manifest
from core.tasks import CancellationToken, TaskCancelled, TaskManager
from storage.database.repository import Repository, SCHEMA_VERSION
from storage.models.candidate import Candidate
from storage.models.source import DataSource


class M2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = Repository(self.root / 'index.db')
        self.calls = []
        self.authorized = True
        self.payload = b'ordinary existing file\x00\xff'
        self.remote = "/sdcard/DCIM/a '$(touch NEVER); image.jpg"

    def runner(self, command, **kwargs):
        self.calls.append((command, kwargs))
        output = ''
        if command[1:] == ['devices', '-l']:
            output = 'List of devices attached\nSERIAL ' + ('device' if self.authorized else 'unauthorized') + '\n'
        elif command[3] == 'shell' and command[4].startswith('find '):
            output = self.remote + '\x00'
        elif command[3] == 'shell' and command[4].startswith('readlink '):
            output = shlex.split(command[4])[-1] + '\n'
        elif command[3] == 'pull':
            Path(command[-1]).write_bytes(self.payload)
        return subprocess.CompletedProcess(command, 0, output, '')

    def adapter(self):
        return AndroidAdapter('adb', self.runner)

    def manager(self):
        manager = TaskManager(self.repo)
        self.addCleanup(manager.shutdown)
        return manager

    def test_migrate_v1_preserves_tasks_and_legacy_payload(self):
        path = self.root / 'v1.db'
        payload = asdict(Candidate('ios_backup', 'old.jpg', 'jpg', 5))
        for name in ('source_id', 'local_path', 'acquired_at', 'source_stat'):
            payload.pop(name)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,error_code TEXT)')
            db.execute('CREATE TABLE candidates(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),payload TEXT NOT NULL)')
            db.execute('CREATE INDEX candidate_task ON candidates(task_id)')
            db.execute("INSERT INTO tasks VALUES('task','ios_manifest','completed','date','date',NULL)")
            db.execute('INSERT INTO candidates VALUES(?,?,?)', (payload['id'], 'task', json.dumps(payload)))
            db.execute('PRAGMA user_version=1')
        migrated = Repository(path)
        self.assertIsNone(migrated.task('task')['source_id'])
        self.assertEqual(migrated.candidate_page('task')[0], payload)
        self.assertEqual(Repository(path).candidate_count('task'), 1)
        with closing(sqlite3.connect(path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)

    def test_source_and_paginated_results_survive_reopen(self):
        source = DataSource('android', 'SERIAL', 'Phone')
        task = self.repo.create_task('android_acquisition', source)
        other = self.repo.create_task('other')
        self.repo.add_candidates(task, [Candidate('android_file', str(i), 'jpg', i) for i in range(205)])
        self.repo.add_candidate(other, Candidate('other', 'excluded', '', 0))
        repo = Repository(self.repo.path)
        self.assertEqual(repo.source(repo.task(task)['source_id'])['locator'], 'SERIAL')
        pages = [repo.candidate_page(task, offset=i, limit=100) for i in (0, 100, 200)]
        self.assertEqual([len(page) for page in pages], [100, 100, 5])
        self.assertEqual([c['name'] for page in pages for c in page], [str(i) for i in range(205)])
        self.assertEqual(repo.candidate_count(task), 205)
        self.assertEqual(repo.candidate_page(task, offset=300), [])
        self.assertEqual(repo.candidate_page('missing'), [])
        for kwargs in ({'offset': -1}, {'limit': 0}, {'limit': 1001}):
            with self.assertRaises(ValueError): repo.candidate_page(task, **kwargs)

    def test_authorized_listing_uses_quoted_read_only_command(self):
        root = "/sdcard/DCIM/$(touch NEVER); 'album'"
        self.remote = root + '/a.jpg'
        self.assertEqual(self.adapter().list_files('SERIAL', root), [self.remote])
        command, kwargs = self.calls[-1]
        self.assertEqual(command, ['adb', '-s', 'SERIAL', 'shell', f'find {shlex.quote(root + "/")} -type f -print0'])
        self.assertNotIn('shell', kwargs)

    def test_unapproved_device_blocks_listing_and_pull(self):
        for state in ('unauthorized', 'offline', 'no permissions'):
            def runner(command, **kwargs):
                self.calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 0, f'SERIAL {state}\n')
            adapter = AndroidAdapter('adb', runner)
            with self.assertRaises(AdbError): adapter.list_files('SERIAL', '/sdcard/DCIM')
            with self.assertRaises(AdbError): adapter.pull_file('SERIAL', self.remote, self.root/'not-created')
        self.assertTrue(all(cmd[1:] == ['devices', '-l'] for cmd, _ in self.calls))

    def test_remote_paths_outside_shared_storage_rejected_before_commands(self):
        for path in ('/data/private', '/sdcard/../data/x', '/sdcardevil/x', 'relative', '//sdcard/a', '/sdcard/x\nfile'):
            with self.assertRaises(AdbError): self.adapter().list_files('SERIAL', path)
        self.assertEqual(self.calls, [])

    def test_listing_outside_root_and_overflow_fail_instead_of_truncating(self):
        for output in ('/sdcard/Download/x\x00', '/sdcard/DCIM/x', '/sdcard/DCIM/a\x00/sdcard/DCIM/b\x00'):
            def runner(command, **kwargs):
                text = 'SERIAL device\n' if command[1] == 'devices' else ('/sdcard/DCIM\n' if command[4].startswith('readlink ') else output)
                return subprocess.CompletedProcess(command, 0, text)
            with self.assertRaises(AdbError): AndroidAdapter('adb', runner).list_files('SERIAL', '/sdcard/DCIM', limit=1)

    def test_selected_copy_hash_provenance_and_no_overwrite(self):
        destination = self.root/'Acquisitions'
        source = DataSource('android', 'SERIAL', 'Phone')
        adapter = self.adapter()
        paths = adapter.list_files('SERIAL', '/sdcard/DCIM')
        handle = self.manager().submit('android_acquisition',
            lambda token: acquire_files(adapter, 'SERIAL', paths, destination, token),
            source=source, persist_candidates=True, batch_size=1)
        self.assertEqual(handle.future.result(5), 1)
        stored = Repository(self.repo.path).candidate_page(handle.id)[0]
        self.assertEqual(stored['source_id'], source.id)
        self.assertEqual(stored['original_path'], self.remote)
        self.assertEqual(stored['sha256'], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(stored['integrity'], 'not_checked')
        self.assertEqual(stored['status'], 'candidate')
        self.assertTrue(stored['acquired_at'])
        copied = Path(stored['local_path'])
        self.assertEqual(copied.read_bytes(), self.payload)
        again = list(acquire_files(adapter, 'SERIAL', paths, destination, CancellationToken()))
        self.assertNotEqual(again[0].local_path, str(copied))
        self.assertEqual(copied.read_bytes(), self.payload)
        pulls = [cmd for cmd, _ in self.calls if len(cmd) > 3 and cmd[3] == 'pull']
        self.assertEqual(pulls[0][4], self.remote)
        self.assertTrue(all('root' not in cmd and 'push' not in cmd for cmd, _ in self.calls))

    def test_selected_only_and_invalid_selection_copies_nothing(self):
        adapter = self.adapter()
        destination = self.root/'Acquisitions'
        for paths in ([], [self.remote, '/data/private']):
            with self.assertRaises(AdbError): list(acquire_files(adapter, 'SERIAL', paths, destination, CancellationToken()))
        self.assertEqual(self.calls, [])
        list(acquire_files(adapter, 'SERIAL', [self.remote, self.remote], destination, CancellationToken()))
        self.assertEqual(len([c for c, _ in self.calls if len(c) > 3 and c[3] == 'pull']), 1)

    def test_pull_failure_timeout_or_cancel_removes_partial_output(self):
        for failure in ('exit', 'timeout', 'cancel', 'directory'):
            token = CancellationToken()
            def runner(command, **kwargs):
                if len(command) > 3 and command[3] == 'pull':
                    path = Path(command[-1])
                    if failure == 'directory':
                        path.mkdir()
                    else:
                        path.write_bytes(b'partial')
                    if failure == 'timeout': raise subprocess.TimeoutExpired(command, 60)
                    if failure == 'cancel': token.cancel()
                    if failure == 'exit': return subprocess.CompletedProcess(command, 1, '', 'private device path')
                    return subprocess.CompletedProcess(command, 0, '', '')
                output = 'SERIAL device\n' if command[1] == 'devices' else (self.remote+'\n' if command[4].startswith('readlink ') else '')
                return subprocess.CompletedProcess(command, 0, output, '')
            dest = self.root/failure
            with self.assertRaises((AdbError, TaskCancelled)):
                list(acquire_files(AndroidAdapter('adb', runner), 'SERIAL', [self.remote], dest, token))
            self.assertEqual(list(dest.iterdir()), [])

    def test_disconnected_device_is_rechecked_before_each_pull(self):
        adapter = self.adapter()
        iterator = acquire_files(adapter, 'SERIAL', [self.remote, '/sdcard/DCIM/b.jpg'], self.root/'copies', CancellationToken())
        first = next(iterator)
        self.authorized = False
        with self.assertRaises(AdbError): next(iterator)
        self.assertEqual(Path(first.local_path).read_bytes(), self.payload)
        self.assertEqual(len([c for c, _ in self.calls if len(c) > 3 and c[3] == 'pull']), 1)

    def test_cancellation_retains_committed_candidates_and_source(self):
        source = DataSource('android', 'SERIAL')
        def operation(token):
            yield Candidate('android_file', 'a.jpg', 'jpg', 1)
            token.cancel()
            token.checkpoint()
        handle = self.manager().submit('android_acquisition', operation, source=source, persist_candidates=True, batch_size=1)
        with self.assertRaises(TaskCancelled): handle.future.result(5)
        self.assertEqual(self.repo.task(handle.id)['status'], 'cancelled')
        self.assertEqual(self.repo.candidate_count(handle.id), 1)
        self.assertEqual(self.repo.candidate_page(handle.id)[0]['source_id'], source.id)

    def test_stream_failure_preserves_committed_batches_and_redacts_error(self):
        def operation(token):
            yield Candidate('test', 'a', '', 1)
            raise AdbError('private serial and path')
        with self.assertLogs('deeprecover', level='WARNING') as logs:
            handle = self.manager().submit('failure', operation, persist_candidates=True, batch_size=1)
            with self.assertRaises(AdbError): handle.future.result(5)
        self.assertNotIn('private', ''.join(logs.output))
        self.assertEqual(self.repo.task(handle.id)['error_code'], 'AdbError')
        self.assertEqual(self.repo.candidate_count(handle.id), 1)

    def test_batch_insert_is_atomic(self):
        task = self.repo.create_task('test')
        candidate = Candidate('test', 'duplicate', '', 0)
        with self.assertRaises(sqlite3.IntegrityError): self.repo.add_candidates(task, [candidate, candidate])
        self.assertEqual(self.repo.candidate_count(task), 0)

    def test_manifest_streams_beyond_old_limit_and_keeps_source_read_only(self):
        backup = self.root/'backup'
        backup.mkdir()
        (backup/'Manifest.plist').write_bytes(plistlib.dumps({'IsEncrypted': False}))
        with closing(sqlite3.connect(backup/'Manifest.db')) as db, db:
            db.execute('CREATE TABLE Files(fileID TEXT PRIMARY KEY,domain TEXT,relativePath TEXT,flags INTEGER) WITHOUT ROWID')
            db.executemany('INSERT INTO Files VALUES(?,?,?,1)',
                ((f'{i:040x}', 'HomeDomain', f'Documents/{i}.jpg') for i in range(10005)))
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in backup.iterdir()}
        source = DataSource('ios_backup', str(backup), 'Backup')
        handle = self.manager().submit('ios_manifest', lambda token: iter_manifest(backup, token), source=source, persist_candidates=True)
        # This is a correctness fixture, not a 20-second indexing SLA. Native
        # Windows CI incurs more SQLite/file-inspection I/O than Linux.
        self.assertEqual(handle.future.result(90 if os.name == 'nt' else 20), 10005)
        self.assertEqual(self.repo.candidate_count(handle.id), 10005)
        self.assertEqual(len(self.repo.candidate_page(handle.id, offset=10000)), 5)
        self.assertEqual(self.repo.candidate_page(handle.id)[0]['source_id'], source.id)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in backup.iterdir()})

    def test_cancel_before_work_starts_does_not_touch_device(self):
        token = CancellationToken(); token.cancel()
        with self.assertRaises(TaskCancelled):
            list(acquire_files(self.adapter(), 'SERIAL', [self.remote], self.root/'copies', token))
        self.assertEqual(self.calls, [])

    def test_symlink_resolving_outside_shared_storage_is_not_read(self):
        calls=[]
        def runner(command, **kwargs):
            calls.append(command)
            output='SERIAL device\n' if command[1]=='devices' else '/data/local/tmp/private\n'
            return subprocess.CompletedProcess(command,0,output,'')
        adapter=AndroidAdapter('adb',runner)
        with self.assertRaises(AdbError): adapter.list_files('SERIAL','/sdcard/link')
        with self.assertRaises(AdbError): adapter.pull_file('SERIAL','/sdcard/link/a',self.root/'copy')
        self.assertFalse(any(len(cmd)>3 and cmd[3]=='pull' for cmd in calls))

    def test_stream_generator_is_closed_on_storage_failure(self):
        closed=[]
        def operation(token):
            try:
                yield Candidate('test','a','',0)
                yield Candidate('test','b','',0)
            finally:
                closed.append(True)
        with patch.object(self.repo,'add_candidates',side_effect=sqlite3.OperationalError('private path')):
            with self.assertLogs('deeprecover',level='WARNING'):
                handle=self.manager().submit('failure',operation,persist_candidates=True,batch_size=1)
                with self.assertRaises(sqlite3.OperationalError): handle.future.result(5)
        self.assertEqual(closed,[True])
