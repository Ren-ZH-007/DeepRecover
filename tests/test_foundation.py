import hashlib
from contextlib import closing
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from core.device.android.adapter import AndroidAdapter, AdbError, parse_devices
from core.device.ios.discovery import discover_backups
from core.parser.ios_backup.manifest import read_manifest, BackupError
from core.tasks import TaskManager, TaskCancelled
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from app.app_config import AppConfig

class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def backup(self, encrypted=False):
        root = self.root / 'backup'; root.mkdir()
        (root/'Manifest.plist').write_bytes(plistlib.dumps({'IsEncrypted':encrypted}))
        (root/'Info.plist').write_bytes(plistlib.dumps({'Device Name':'Test iPhone','Product Version':'18.0'}))
        fid = 'a'*40
        (root/'aa').mkdir(); (root/'aa'/fid).write_bytes(b'test-data')
        with closing(sqlite3.connect(root/'Manifest.db')) as db, db:
            db.execute('CREATE TABLE Files(fileID TEXT,domain TEXT,relativePath TEXT,flags INTEGER)')
            db.executemany('INSERT INTO Files VALUES(?,?,?,?)',[(fid,'CameraRollDomain','Media/photo.jpg',1),('b'*40,'HomeDomain','missing.txt',1),('../../bad','HomeDomain','escape',1),('c'*40,'HomeDomain','folder',2)])
        return root
    def test_candidate_invariants(self):
        for kwargs in ({'size':-1},{'size':1,'confidence':2},{'size':1,'offset':-1}):
            with self.assertRaises(ValueError): Candidate('test','name','jpg',**kwargs)
    def test_database_migration_persistence_and_interruption(self):
        path=self.root/'index.db'; repo=Repository(path)
        task=repo.create_task('scan'); repo.update_task(task,'running')
        candidate=Candidate('image','photo.jpg','jpg',9)
        repo.add_candidate(task,candidate)
        repo=Repository(path); repo.recover_interrupted()
        self.assertEqual(repo.tasks()[0]['status'],'interrupted')
        self.assertEqual(repo.candidates(task)[0]['id'],candidate.id)
    def test_future_database_rejected(self):
        path=self.root/'index.db'
        with closing(sqlite3.connect(path)) as db, db: db.execute('PRAGMA user_version=99')
        with self.assertRaises(RuntimeError): Repository(path)
    def test_adb_states(self):
        result=parse_devices('List of devices attached\nA device model:Pixel_8\nB unauthorized\nC offline\nD no permissions (udev)\n')
        self.assertEqual([d.state for d in result],['device','unauthorized','offline','no permissions'])
        self.assertEqual(result[0].model,'Pixel 8')
        self.assertFalse(result[1].authorized)
    def test_adb_command_and_failure(self):
        calls=[]
        def run(command,**kwargs):
            calls.append((command,kwargs));return subprocess.CompletedProcess(command,0,'List of devices attached\n')
        self.assertEqual(AndroidAdapter('adb',run).devices(),[])
        self.assertEqual(calls[0][0],['adb','devices','-l'])
        self.assertEqual(calls[0][1]['timeout'],15)
        self.assertNotIn('shell',calls[0][1])
        def timeout(*a,**kw): raise subprocess.TimeoutExpired('adb',15)
        with self.assertRaises(AdbError): AndroidAdapter('adb',timeout).devices()
    def test_missing_adb(self):
        with patch.dict(os.environ,{},clear=True),patch('shutil.which',return_value=None):
            with self.assertRaises(AdbError): AndroidAdapter().devices()
    def test_backup_discovery_and_read_only(self):
        root=self.backup()
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
        backups=discover_backups([self.root,root])
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].device_name,'Test iPhone')
        result=read_manifest(root)
        self.assertEqual(len(result),2)
        self.assertEqual(result[0].size,9)
        self.assertEqual(result[1].integrity,'missing')
        self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()})
    def test_encrypted_backup_rejected(self):
        with self.assertRaises(BackupError): read_manifest(self.backup(True))
    def test_corrupt_backup_rejected(self):
        root=self.backup(); (root/'Manifest.plist').write_bytes(b'invalid')
        self.assertTrue(discover_backups([root])[0].error)
        with self.assertRaises(BackupError): read_manifest(root)
    def test_corrupt_database_rejected(self):
        root=self.backup(); (root/'Manifest.db').write_bytes(b'invalid')
        with self.assertRaises(BackupError): read_manifest(root)
    def test_manifest_limit(self):
        self.assertEqual(len(read_manifest(self.backup(),limit=1)),1)
    def test_tasks_success_failure_cancel(self):
        repo=Repository(self.root/'index.db'); manager=TaskManager(repo)
        try:
            self.assertEqual(manager.submit('success',lambda t:42).future.result(3),42)
            def fail(t): raise ValueError('private data not logged')
            with self.assertRaises(ValueError): manager.submit('failure',fail).future.result(3)
            def cancel(t): t.cancel();t.checkpoint()
            with self.assertRaises(TaskCancelled): manager.submit('cancel',cancel).future.result(3)
            self.assertEqual({t['status'] for t in repo.tasks()},{'completed','failed','cancelled'})
            self.assertEqual(next(t for t in repo.tasks() if t['status']=='failed')['error_code'],'ValueError')
        finally: manager.shutdown()
    def test_batch_candidates_persist_before_completion(self):
        repo=Repository(self.root/"index.db"); manager=TaskManager(repo)
        try:
            handle=manager.submit("index",lambda t:[Candidate("test","a","txt",1)],persist_candidates=True)
            handle.future.result(3)
            self.assertEqual(len(repo.candidates(handle.id)),1)
            self.assertEqual(repo.tasks()[0]["status"],"completed")
        finally: manager.shutdown()

    def test_config_initialization(self):
        config=AppConfig(self.root/'data'); config.initialize()
        self.assertTrue((config.data_dir/'Recovered').is_dir())

if __name__=='__main__': unittest.main()
