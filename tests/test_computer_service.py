"""Generated volumes exercise scan -> persistent history -> verified export."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.app_config import AppConfig
from core.candidate_io import candidate_input, read_candidate, CandidateAccessError
from core.computer.content import checked_extents
from core.computer.io import DiskError
from core.computer.service import ComputerScanner, ComputerError, inspect_source, check_access
from core.export.service import export_batch, ExportError
from core.tasks import CancellationToken, TaskManager, TaskCancelled
from storage.database.repository import Repository
from storage.models.source import DataSource
from tests.exfat_fixture import ExfatImage, entry_set
from tests.fixtures_ntfs import NTFSFixture, file_name, resident_attr, nonresident_attr
from tests.test_scanner import png_image, JPEG


class ComputerServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = AppConfig(self.root / 'work'); self.config.initialize()
        self.repo = Repository(self.config.database)
        self.manager = TaskManager(self.repo); self.addCleanup(self.manager.shutdown)

    def scan(self, data, mode='deleted', **options):
        path = self.root / '源 镜像.img'; path.write_bytes(data)
        source = DataSource('computer', str(path))
        task = self.manager.submit('computer_scan', lambda token: ComputerScanner(self.config.data_dir, **options).scan(path, token, mode=mode),
                                   persist_candidates=True, source=source, batch_size=1)
        task.future.result(10)
        self.assertEqual(path.read_bytes(), bytes(data))
        return path, task.id, self.repo.candidate_ids(task.id)

    def export(self, ids):
        task = self.manager.submit('export', lambda token, tid: export_batch(self.repo, tid, ids, self.root / 'output', self.config.data_dir, token), with_task_id=True)
        task.future.result(10)
        return self.repo.exports(task.id)

    def test_exfat_deleted_file_preview_bytes_export_and_history(self):
        image = ExfatImage(offset=0); payload = png_image()
        image.write_cluster(5, payload); image.finish_root([entry_set('已删除 图片.png', first=5, size=len(payload))])
        path, task, ids = self.scan(image.data)
        candidate = self.repo.candidate(ids[0])
        self.assertEqual((candidate['deletion_state'], candidate['file_system']), ('deleted_record', 'exFAT'))
        self.assertEqual(read_candidate(self.repo, ids[0], self.config.data_dir, CancellationToken()), payload)
        records = self.export(ids)
        self.assertEqual(Path(records[0]['output_path']).read_bytes(), payload)
        self.assertEqual(records[0]['source_sha256'], records[0]['target_sha256'])
        self.assertEqual(records[0]['status'], 'exported')
        path.unlink()
        reopened = Repository(self.repo.path)
        self.assertEqual(reopened.candidate_count(task), 1)
        self.assertEqual(reopened.exports(records[0]['task_id'])[0]['metadata']['deletion_state'], 'deleted_record')

    def test_ntfs_resident_sector_fixups_are_reconstructed_on_export(self):
        image = NTFSFixture(); payload = bytes(range(256)) * 2
        image.write_record(16, [resident_attr(0x30, file_name('resident.bin')), resident_attr(0x80, payload)])
        _path, _task, ids = self.scan(image.image)
        self.assertGreater(len(self.repo.candidate(ids[0])['extents']), 1)
        self.assertEqual(Path(self.export(ids)[0]['output_path']).read_bytes(), payload)

    def test_ntfs_fragmented_file_uses_logical_not_physical_order(self):
        image = NTFSFixture(); payload = b'A' * 4096 + b'B' * 777
        image.write_cluster(120, payload[:4096]); image.write_cluster(100, payload[4096:])
        image.write_record(16, [resident_attr(0x30, file_name('fragmented.txt')), nonresident_attr([(120, 1), (100, 1)], len(payload))])
        _path, _task, ids = self.scan(image.image)
        self.assertEqual(read_candidate(self.repo, ids[0], self.config.data_dir, CancellationToken()), payload)
        self.assertEqual(Path(self.export(ids)[0]['output_path']).read_bytes(), payload)

    def test_deleted_zero_byte_file_is_valid_and_not_mistaken_for_missing_layout(self):
        image = ExfatImage(offset=0); image.finish_root([entry_set('empty.txt', first=0, size=0)])
        _path, _task, ids = self.scan(image.data)
        self.assertEqual(self.repo.candidate(ids[0])['sha256'], hashlib.sha256(b'').hexdigest())
        self.assertEqual(Path(self.export(ids)[0]['output_path']).read_bytes(), b'')

    def test_partial_record_is_visible_but_cannot_export_as_complete(self):
        image = ExfatImage(offset=0); image.finish_root([entry_set(contiguous=False)])
        _path, _task, ids = self.scan(image.data)
        self.assertEqual(self.repo.candidate(ids[0])['status'], 'partial')
        with self.assertRaises(CandidateAccessError): candidate_input(self.repo, ids[0], self.config.data_dir)

    def test_signature_candidates_explicitly_have_unknown_deletion_state(self):
        _path, _task, ids = self.scan(b'padding' + png_image() + JPEG, mode='signature')
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(self.repo.candidate(i)['deletion_state'] == 'unknown' for i in ids))
        self.assertEqual([Path(r['output_path']).read_bytes() for r in self.export(ids)], [png_image(), JPEG])

    def test_changed_image_is_rejected_even_when_candidate_bytes_are_unchanged(self):
        path, _task, ids = self.scan(b'prefix' + png_image(), mode='signature')
        path.write_bytes(b'change' + png_image())
        with self.assertRaises(CandidateAccessError): read_candidate(self.repo, ids[0], self.config.data_dir, CancellationToken())

    def test_unknown_filesystem_does_not_silently_become_deleted_recovery(self):
        path = self.root / 'APFS.img'; path.write_bytes(bytes(32) + b'NXSB' + bytes(1024))
        self.assertEqual(inspect_source(path)[0].file_system, 'APFS')
        with self.assertRaises(ComputerError): list(ComputerScanner().scan(path, CancellationToken()))

    def test_source_limits_and_cancellation_are_explicit(self):
        path = self.root / 'file.img'; path.write_bytes(png_image() + JPEG)
        token = CancellationToken(); token.cancel()
        with self.assertRaises(TaskCancelled): list(ComputerScanner().scan(path, token, mode='signature'))
        with self.assertRaises(ComputerError): list(ComputerScanner(max_candidates=1).scan(path, CancellationToken(), mode='signature'))

    def test_extent_metadata_cannot_escape_repeat_or_truncate_source(self):
        for extents, size in [([(-1, 1)], 1), ([(0, 5)], 4), ([(0, 4), (2, 2)], 6), ([(8, 4)], 4), ([(True, 1)], 1)]:
            with self.subTest(extents=extents), self.assertRaises(ValueError): checked_extents(extents, size, 10)

    def test_raw_source_checks_work_directory_before_opening_source(self):
        with patch('core.computer.service.ensure_different_device', side_effect=DiskError('same disk')) as guard, \
             patch('core.computer.service.DiskReader') as reader:
            with self.assertRaises(DiskError): list(ComputerScanner(self.config.data_dir).scan('/dev/disk99', CancellationToken(), kind='device'))
            guard.assert_called_once(); reader.assert_not_called()
        with self.assertRaises(ComputerError): list(ComputerScanner().scan('/dev/disk99', CancellationToken(), kind='device'))
