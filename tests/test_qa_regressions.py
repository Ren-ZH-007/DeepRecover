"""2026-11-03 detailed QA regressions: source protection and resource lifetime."""
import gc
import hashlib
import os
from pathlib import Path
import plistlib
import sqlite3
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch
import weakref

from app.app_config import AppConfig
from core.candidate_io import snapshot
from core.export.service import export_batch, ExportError
from core.parser.ios_backup.manifest import read_manifest, iter_manifest, BackupError
from core.tasks import CancellationToken, TaskManager, TaskCancelled
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from storage.models.source import DataSource


class DetailedQaRegressions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=AppConfig(self.root/'data'); self.config.initialize()
        self.repo=Repository(self.config.database)
        self.manager=TaskManager(self.repo); self.addCleanup(self.manager.shutdown)

    def backup(self, wal=False):
        root=self.root/'backup'; root.mkdir()
        (root/'Manifest.plist').write_bytes(plistlib.dumps({'IsEncrypted':False}))
        db=sqlite3.connect(root/'Manifest.db')
        # WAL cases deliberately retain this handle; close before directory
        # cleanup even when a test assertion fails before its explicit close.
        self.addCleanup(db.close)
        if wal:
            db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('CREATE TABLE Files(fileID TEXT,domain TEXT,relativePath TEXT,flags INTEGER)')
        db.executemany('INSERT INTO Files VALUES(?,?,?,1)',[(char*40,'HomeDomain',char+'.jpg') for char in ('a','b')])
        db.commit()
        return root,db

    @staticmethod
    def hashes(root):
        return {str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob('*') if path.is_file()}

    def android(self):
        path=self.config.data_dir/'Acquisitions'/'content'; path.write_bytes(b'ordinary file')
        source=DataSource('android','QA-SYNTHETIC')
        task=self.repo.create_task('android_acquisition',source)
        candidate=Candidate('android_file','existing.txt','txt',path.stat().st_size,local_path=str(path),source_id=source.id,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),source_stat=snapshot(path.stat()))
        self.repo.add_candidate(task,candidate); self.repo.update_task(task,'completed')
        return candidate

    def test_any_registered_backup_is_protected_from_unrelated_export(self):
        candidate=self.android()
        root,db=self.backup(); db.close()
        registered=self.repo.create_task('ios_manifest',DataSource('ios_backup',str(root)))
        self.repo.update_task(registered,'completed')
        before=self.hashes(root)
        task=self.repo.create_task('export')
        with self.assertRaises(ExportError):
            export_batch(self.repo,task,[candidate.id],root/'should-not-be-created',self.config.data_dir,CancellationToken())
        self.assertFalse((root/'should-not-be-created').exists())
        self.assertEqual(self.hashes(root),before)

    def test_finished_preview_frames_are_not_retained_by_task_manager(self):
        class Frame:
            def __init__(self): self.pixels=bytearray(512*1024)
        references=[]
        for _ in range(12):
            release,finished=Event(),Event()
            def operation(token,release=release):
                if not release.wait(3): raise RuntimeError('test gate not released')
                frame=Frame(); references.append(weakref.ref(frame)); return frame
            handle=self.manager.submit('image_preview',operation)
            handle.future.add_done_callback(lambda _,event=finished:event.set())
            release.set(); handle.future.result(3); self.assertTrue(finished.wait(3))
        gc.collect()
        retained=sum(reference() is not None for reference in references)
        self.assertLessEqual(retained,1,f'{retained} completed preview frames are still retained')

    def test_nonempty_wal_is_rejected_without_modifying_source_files(self):
        root,db=self.backup(wal=True)
        try:
            before=self.hashes(root)
            with self.assertRaises(BackupError): read_manifest(root)
            self.assertEqual(self.hashes(root),before)
        finally:
            db.close()

    def test_closed_wal_database_creates_no_source_sidecars(self):
        root,db=self.backup(wal=True); db.close()
        before=self.hashes(root)
        iterator=iter_manifest(root)
        try:
            next(iterator)
            self.assertEqual(self.hashes(root),before)
        finally:
            iterator.close()
        self.assertEqual(self.hashes(root),before)

    def test_nonempty_rollback_journal_is_rejected_without_source_writes(self):
        root,db=self.backup(); db.close()
        (root/'Manifest.db-journal').write_bytes(b'pending transaction evidence')
        before=self.hashes(root)
        with self.assertRaises(BackupError): read_manifest(root)
        self.assertEqual(self.hashes(root),before)

    @unittest.skipIf(os.name=='nt','Creating symlinks may need Windows privileges')
    def test_manifest_symlink_cannot_escape_selected_backup(self):
        root,db=self.backup(); db.close()
        outside=self.root/'external.db'
        (root/'Manifest.db').replace(outside)
        (root/'Manifest.db').symlink_to(outside)
        with self.assertRaises(BackupError): read_manifest(root)

    def test_manifest_identity_change_during_iteration_is_not_completed(self):
        root,db=self.backup(); db.close()
        iterator=iter_manifest(root); next(iterator)
        path=root/'Manifest.db'; info=path.stat()
        os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns+1_000_000))
        with self.assertRaises(BackupError): list(iterator)

    def test_checkpointed_unicode_backup_keeps_exact_source_inventory(self):
        root,db=self.backup(wal=True); db.close()
        renamed=self.root/'iPhone 备份 #1'; root.rename(renamed)
        before=self.hashes(renamed)
        self.assertEqual(len(read_manifest(renamed)),2)
        self.assertEqual(self.hashes(renamed),before)

    def test_exact_bulk_selection_limit_rejects_10001_without_writes(self):
        task=self.repo.create_task('test')
        self.repo.add_candidates(task,[Candidate('test',str(i),'txt',0) for i in range(10001)])
        with self.assertRaises(ValueError): self.repo.candidate_ids(task)
        self.assertEqual(self.repo.candidate_count(task),10001)
        self.assertEqual(self.repo.export_count(task),0)

    def test_shutdown_cancels_live_job_without_deadlocking_handle_cleanup(self):
        started=Event()
        def operation(token):
            started.set()
            while not Event().wait(0.01): token.checkpoint()
        handle=self.manager.submit('test',operation)
        self.assertTrue(started.wait(3))
        closer=Thread(target=self.manager.shutdown); closer.start()
        closer.join(3)
        self.assertFalse(closer.is_alive())
        with self.assertRaises(TaskCancelled): handle.future.result(3)
