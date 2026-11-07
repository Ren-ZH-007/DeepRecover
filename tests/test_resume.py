"""Interrupted signature scans continue into the same task with identical results."""
import json
from contextlib import closing
import os
from pathlib import Path
import random
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from app.app_config import AppConfig
from core.computer.service import ComputerScanner
from core.scanner.resume import ResumeError, plan_resume, resume_operation, verify_resume
from core.scanner.signature.scanner import ScanError, SignatureScanner
from core.tasks import CancellationToken, ScanCheckpoint, TaskCancelled, TaskManager
from storage.database.repository import Repository
from storage.models.source import DataSource
from tests.pdf_fixture import make_pdf
from tests.test_scanner import JPEG, png_image
from tests.video_signature_fixture import movie


def source_bytes(size):
    rng = random.Random(11)
    blobs = [png_image(), JPEG, make_pdf(), movie(), b'%PDF-broken', png_image()[:30], JPEG]
    data, at = bytearray(), 0
    while len(data) < size:
        data += bytes(rng.randrange(256) for _ in range(rng.randrange(1500, 60000)))
        data += blobs[at % len(blobs)]; at += 1
    # A container straddling the 1 MiB block boundary used by the default scanners.
    data[1024 * 1024 - 40:1024 * 1024 - 40] = make_pdf()
    return bytes(data)


def interrupt_after(items, token, limit):
    for index, item in enumerate(items):
        yield item
        if index + 1 >= limit:
            token.cancel()


def summary(rows):
    return [(r['offset'], r['size'], r['integrity'], r['status'], r['sha256'], r['scan_next']) for r in rows]


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = AppConfig(self.root / 'work'); self.config.initialize()
        self.repo = Repository(self.config.database)
        self.manager = TaskManager(self.repo); self.addCleanup(self.manager.shutdown)

    def run_task(self, kind, operation, source, resume_task=None):
        handle = self.manager.submit(kind, operation, persist_candidates=True, source=source, batch_size=1,
                                     resume_task=resume_task)
        try:
            handle.future.result(60)
        except TaskCancelled:
            pass
        return handle.id

    def resume(self, task):
        plan = plan_resume(self.repo, task)
        verify_resume(self.repo, plan, self.config.data_dir)
        self.run_task(plan.kind, resume_operation(plan, self.config.data_dir), SimpleNamespace(id=plan.source_id), task)
        return plan

    # -- image signature scans ------------------------------------------------
    def image_scan(self, path, limit=None, block_size=4096):
        source = DataSource('image', str(path), path.name)
        def operation(token):
            items = SignatureScanner(block_size=block_size).scan(path, token, checkpoint_every=0)
            return interrupt_after(items, token, limit) if limit else items
        return self.run_task('signature_scan', operation, source)

    def test_every_interruption_point_resumes_to_identical_results(self):
        path = self.root / 'disk.img'; path.write_bytes(source_bytes(2_400_000))
        reference = summary(self.repo.candidates(self.image_scan(path)))
        self.assertGreater(len(reference), 30)
        items = len(list(SignatureScanner(block_size=4096).scan(path, CancellationToken(), checkpoint_every=0)))
        for limit in sorted({1, 2, 3, items // 3, items // 2, items - 2, items - 1}):
            with self.subTest(limit=limit):
                task = self.image_scan(path, limit)
                self.assertEqual(self.repo.task(task)['status'], 'cancelled')
                before = len(self.repo.candidates(task))
                self.assertLessEqual(before, len(reference))  # the last points have every candidate stored
                plan = self.resume(task)
                self.assertEqual(self.repo.task(task)['status'], 'completed')
                self.assertEqual(summary(self.repo.candidates(task)), reference)
                self.assertGreaterEqual(plan.position, self.repo.candidates(task)[before - 1]['scan_next'] if before else 0)

    def test_crash_marked_interrupted_and_repeated_resumes_still_match(self):
        path = self.root / 'disk.img'; path.write_bytes(source_bytes(1_500_000))
        reference = summary(self.repo.candidates(self.image_scan(path)))
        task = self.image_scan(path, 5)
        self.repo.update_task(task, 'interrupted')
        plan = plan_resume(self.repo, task)
        # Interrupt the resumed run too, then continue again.
        self.run_task('signature_scan', lambda token: interrupt_after(resume_operation(plan, self.config.data_dir)(token), token, 9),
                      SimpleNamespace(id=plan.source_id), task)
        self.assertEqual(self.repo.task(task)['status'], 'cancelled')
        self.resume(task)
        self.assertEqual(summary(self.repo.candidates(task)), reference)
        self.assertEqual(self.repo.scan_checkpoint(task)['candidates'] <= len(reference), True)

    def test_changed_source_is_refused_before_scanning(self):
        path = self.root / 'disk.img'; data = source_bytes(800_000); path.write_bytes(data)
        task = self.image_scan(path, 6)
        count = self.repo.candidate_count(task)
        with path.open('ab') as stream: stream.write(b'x')
        plan = plan_resume(self.repo, task)
        with self.assertRaisesRegex(ResumeError, '源文件已变化'):
            verify_resume(self.repo, plan, self.config.data_dir)
        with self.assertRaises(ScanError):
            list(resume_operation(plan, self.config.data_dir)(CancellationToken()))
        self.assertEqual(self.repo.candidate_count(task), count)

    def test_content_change_behind_an_unchanged_identity_is_caught_by_rehashing(self):
        path = self.root / 'disk.img'; data = bytearray(source_bytes(800_000)); path.write_bytes(data)
        task = self.image_scan(path, 12)
        verified = next(c for c in self.repo.recent_candidates(task, 200) if c['sha256'])
        data[verified['offset'] + verified['size'] // 2] ^= 0xFF; path.write_bytes(data)
        # Simulate a raw device whose identity does not change on writes.
        with closing(sqlite3.connect(self.config.database)) as db, db:
            from core.candidate_io import snapshot
            db.execute('UPDATE scan_checkpoints SET identity=? WHERE task_id=?', (json.dumps(snapshot(path.stat()), sort_keys=True), task))
        with self.assertRaisesRegex(ResumeError, '来源内容与已保存的候选不一致'):
            verify_resume(self.repo, plan_resume(self.repo, task), self.config.data_dir)

    def test_tasks_that_cannot_resume_are_explained(self):
        path = self.root / 'disk.img'; path.write_bytes(source_bytes(300_000))
        done = self.image_scan(path)
        with self.assertRaisesRegex(ResumeError, '已完成'):
            plan_resume(self.repo, done)
        legacy = self.run_task('signature_scan', lambda token: interrupt_after(SignatureScanner().scan(path, token), token, 2),
                               DataSource('image', str(path)))
        with self.assertRaisesRegex(ResumeError, '没有断点记录'):
            plan_resume(self.repo, legacy)
        export = self.repo.create_task('export'); self.repo.update_task(export, 'cancelled')
        with self.assertRaisesRegex(ResumeError, '只有签名扫描'):
            plan_resume(self.repo, export)
        with self.assertRaisesRegex(ResumeError, '不存在'):
            plan_resume(self.repo, 'missing')
        broken = self.image_scan(path, 4)
        with closing(sqlite3.connect(self.config.database)) as db, db:
            db.execute('UPDATE scan_checkpoints SET candidates=candidates+50 WHERE task_id=?', (broken,))
        with self.assertRaisesRegex(ResumeError, '不一致'):
            plan_resume(self.repo, broken)
        with self.assertRaises(ValueError):
            self.manager.submit('signature_scan', lambda token: iter(()), resume_task=done)

    def test_checkpoints_only_move_forward_for_the_same_source(self):
        task = self.repo.create_task('signature_scan')
        mark = lambda at, identity={'ino': 1}: ScanCheckpoint(at, 0, 100, identity, {'scanner': 'signature'})
        self.repo.save_checkpoint(task, mark(40))
        self.repo.save_checkpoint(task, mark(10))
        self.repo.save_checkpoint(task, mark(90, {'ino': 2}))
        self.assertEqual(self.repo.scan_checkpoint(task)['position'], 40)
        self.repo.save_checkpoint(task, mark(70))
        self.assertEqual(self.repo.scan_checkpoint(task)['position'], 70)
        for bad in (ScanCheckpoint(101, 0, 100, {}, {}), ScanCheckpoint(-1, 0, 100, {}, {}), ScanCheckpoint(1, 0, 100, [], {})):
            with self.assertRaises(ValueError): self.repo.save_checkpoint(task, bad)

    def test_scanner_rejects_bad_resume_arguments(self):
        path = self.root / 'a.img'; path.write_bytes(b'x' * 100)
        for kwargs in ({'start': -1}, {'start': 1.5}, {'start_count': -1}):
            with self.assertRaises(ValueError): list(SignatureScanner().scan(path, CancellationToken(), **kwargs))
        with self.assertRaises(ScanError): list(SignatureScanner().scan(path, CancellationToken(), start=101))
        with self.assertRaises(ScanError): list(SignatureScanner().scan(path, CancellationToken(), expected={'size': 1}))
        plain = list(SignatureScanner().scan(path, CancellationToken()))
        self.assertFalse(any(isinstance(item, ScanCheckpoint) for item in plain))

    # -- computer signature scans ----------------------------------------------
    def computer_scan(self, path, limit=None):
        source = DataSource('computer', str(path), 'raw')
        def operation(token):
            items = ComputerScanner(self.config.data_dir).scan(path, token, kind='image', mode='signature',
                                                                partition_offset=0, checkpoint_every=0)
            return interrupt_after(items, token, limit) if limit else items
        return self.run_task('computer_scan', operation, source)

    def test_computer_signature_scan_resumes_to_identical_results(self):
        path = self.root / '源 镜像.img'; path.write_bytes(source_bytes(3_300_000))
        reference = summary(self.repo.candidates(self.computer_scan(path)))
        self.assertGreater(len(reference), 30)
        for limit in (1, 4, len(reference) // 2, len(reference) + 2):
            with self.subTest(limit=limit):
                task = self.computer_scan(path, limit)
                self.assertEqual(self.repo.task(task)['status'], 'cancelled')
                plan = self.resume(task)
                self.assertEqual(plan.params['scanner'], 'computer_signature')
                self.assertEqual(self.repo.task(task)['status'], 'completed')
                self.assertEqual(summary(self.repo.candidates(task)), reference)

    def test_computer_resume_refuses_changed_image_and_deleted_mode(self):
        path = self.root / 'disk.img'; path.write_bytes(source_bytes(1_200_000))
        task = self.computer_scan(path, 3)
        os.utime(path, ns=(1, 1))
        with self.assertRaisesRegex(ResumeError, '来源身份'):
            verify_resume(self.repo, plan_resume(self.repo, task), self.config.data_dir)
        unmarked = self.run_task('computer_scan', lambda token: interrupt_after(
            ComputerScanner(self.config.data_dir).scan(path, token, kind='image', mode='signature', partition_offset=0), token, 1),
            DataSource('computer', str(path)))
        with self.assertRaisesRegex(ResumeError, '没有断点记录'):
            plan_resume(self.repo, unmarked)
        with self.assertRaises(Exception):
            list(ComputerScanner().scan(path, CancellationToken(), mode='deleted', checkpoint_every=0))


if __name__ == '__main__':
    unittest.main()
