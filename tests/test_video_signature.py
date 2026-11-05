"""Adversarial range checks and scan -> preview/export -> history integration."""
from dataclasses import replace
import hashlib
from importlib.resources import files
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from app.app_config import AppConfig
from core.candidate_io import CandidateAccessError, candidate_input
from core.computer.io import DiskReader
from core.computer.partitions import Partition
from core.computer.service import ComputerScanner
from core.export.service import export_batch
from core.preview.video import prepare_video
from core.scanner.signature.scanner import SignatureScanner, ScanError
from core.scanner.video.validation import validate, MAX_ENTRIES
from core.tasks import CancellationToken, TaskCancelled
from storage.database.repository import Repository
from storage.models.source import DataSource
from tests.test_scanner import png_image
from tests.video_signature_fixture import movie, atom, edit_box, mov_fixture
from ui.recovery.presentation import content_check, deletion_evidence, video_check_details


class VideoStructureTests(unittest.TestCase):
    def check(self, data, *, start=0, end=None, limit=64 * 1024 * 1024, token=None):
        return validate(io.BytesIO(data), start, len(data) if end is None else end, token or CancellationToken(), limit)

    def test_real_h264_aac_fixture_exact_boundary_without_trailing_junk(self):
        data = files('app').joinpath('assets/preview-test.mp4').read_bytes()
        result = self.check(b'prefix' + data + b'unrelated', start=6)
        self.assertEqual((result.size, result.integrity, result.file_type), (len(data), 'video_structure_checked', 'mp4'))
        self.assertEqual(result.details['tracks'], 2)
        self.assertFalse(result.details['bitstream_decoded'])

    def test_moov_before_and_after_media_64bit_offsets_and_box_sizes(self):
        for last in (False, True):
            for co64 in (False, True):
                for large in (False, True):
                    for multiple in (False, True):
                        with self.subTest(last=last, co64=co64, large=large, multiple=multiple):
                            data = movie(mov=True, last=last, co64=co64, large=large, multiple=multiple)
                            result = self.check(data + b'not part of movie')
                            self.assertEqual((result.size, result.integrity, result.file_type), (len(data), 'video_structure_checked', 'mov'))
                            self.assertEqual(result.details['media_boxes'], 2 if multiple else 1)

    def test_truncation_and_partition_end_are_not_assumed_file_boundaries(self):
        data = movie(last=True)
        for end in (7, 19, len(data) - 1):
            self.assertEqual(self.check(data, end=end).integrity, 'video_truncated')
        data = movie()
        self.assertEqual(self.check(data, end=len(data) - 1).integrity, 'video_truncated')
        self.assertEqual(self.check(b'padding' + data, start=7, end=7 + len(data) - 1).integrity, 'video_truncated')

    def test_zero_sized_media_has_unknown_boundary(self):
        data = movie()
        at = data.index(b'mdat') - 4
        data = data[:at] + b'\0' * 4 + data[at + 4:]
        self.assertEqual(self.check(data).integrity, 'video_boundary_unknown')

    def test_chunk_offsets_cannot_point_into_headers_metadata_or_across_media(self):
        data = movie(); mdat = data.index(b'mdat') + 4
        for offsets in ((0, mdat + 4), (mdat - 4, mdat + 4), (mdat + 2, mdat + 6), (mdat, mdat + 100)):
            bad = edit_box(data, b'stco', lambda p: p[:8] + struct.pack('>II', *offsets))
            result = self.check(bad)
            self.assertNotEqual(result.integrity, 'video_structure_checked')
            self.assertIsNone(result.details)

    def test_overlapping_chunk_ranges_are_invalid(self):
        data = movie(); mdat = data.index(b'mdat') + 4
        bad = edit_box(data, b'stco', lambda p: p[:8] + struct.pack('>II', mdat, mdat + 2))
        self.assertEqual(self.check(bad).integrity, 'video_invalid_structure')

    def test_sample_counts_mapping_and_timing_must_agree(self):
        changes = [
            (b'stts', lambda p: p[:8] + struct.pack('>II', 1, 1000)),
            (b'stsc', lambda p: p[:8] + struct.pack('>III', 2, 1, 1)),
            (b'stsc', lambda p: p[:8] + struct.pack('>III', 1, 3, 1)),
            (b'stsc', lambda p: p[:8] + struct.pack('>III', 1, 1, 2)),
            (b'stsz', lambda p: p[:8] + struct.pack('>I', 0)),
            (b'stco', lambda p: p + b'\0'),
            (b'stbl', lambda p: p + atom(b'stts', b'\0' * 8)),
        ]
        for kind, change in changes:
            with self.subTest(kind=kind, change=change):
                self.assertEqual(self.check(edit_box(movie(), kind, change)).integrity, 'video_invalid_structure')

    def test_fragmented_encrypted_external_unknown_layout_are_distinct(self):
        cases = [
            (edit_box(movie(), b'moov', lambda p: p + atom(b'mvex', b'')), 'fragmented'),
            (edit_box(movie(), b'moov', lambda p: p + atom(b'pssh', b'')), 'encrypted'),
            (movie().replace(b'avc1', b'encv'), 'encrypted'),
            (edit_box(movie(), b'dref', lambda p: p[:-1] + b'\0'), 'external_reference'),
            (edit_box(movie(), b'moov', lambda p: p + atom(b'rmra', b'')), 'external_reference'),
            (movie().replace(b'avc1', b'hvc1'), 'unsupported'),
            (edit_box(movie(), b'moov', lambda p: p + atom(b'abcd', b'')), 'unsupported'),
        ]
        for data, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.check(data).integrity, 'video_' + reason)

    def test_bad_avc_configuration_and_missing_video_track_are_rejected(self):
        data = movie(); at = data.index(b'avcC') + 4
        bad = data[:at] + b'\0' + data[at + 1:]
        self.assertEqual(self.check(bad).integrity, 'video_invalid_structure')
        self.assertEqual(self.check(data.replace(b'vide', b'text')).integrity, 'video_unsupported')

    def test_limits_precede_large_allocations_and_cancellation_propagates(self):
        data = movie()
        self.assertEqual(self.check(data, limit=len(data) - 1).integrity, 'video_validation_limit')
        oversized = edit_box(data, b'stsz', lambda p: p[:8] + struct.pack('>I', MAX_ENTRIES + 1))
        self.assertEqual(self.check(oversized).integrity, 'video_validation_limit')
        token = CancellationToken(); token.cancel()
        with self.assertRaises(TaskCancelled): self.check(data, token=token)

    def test_stream_never_reads_media_payload_and_stays_inside_partition(self):
        data = movie(); media = data.index(b'mdat') + 4
        class Watched(io.BytesIO):
            def read(self, size=-1):
                self_start = self.tell()
                if self_start >= media:
                    raise AssertionError('media payload must not be read during structure checks')
                if self_start + size > len(data):
                    raise AssertionError('read outside partition')
                return super().read(size)
        result = validate(Watched(data + b'other partition'), 0, len(data), CancellationToken(), 64 * 1024 * 1024)
        self.assertEqual(result.integrity, 'video_structure_checked')

    def test_group_description_indices_cannot_reference_missing_descriptions(self):
        # Matching sample count, but description 1 does not exist.
        group = atom(b'sbgp', b'\0' * 4 + b'roll' + struct.pack('>III', 1, 2, 1))
        bad = edit_box(movie(), b'stbl', lambda p: p + group)
        self.assertEqual(self.check(bad).integrity, 'video_invalid_structure')

    def test_malformed_box_lengths_do_not_escape_parent_or_source(self):
        data = movie()
        for length in (1, 7, 0xffffffff):
            at = data.index(b'stsc') - 4
            bad = data[:at] + struct.pack('>I', length) + data[at + 4:]
            self.assertEqual(self.check(bad).integrity, 'video_invalid_structure')

    def test_metadata_budget_and_movie_box_count_are_bounded(self):
        with patch('core.scanner.video.validation.MAX_METADATA', 32):
            self.assertEqual(self.check(movie()).integrity, 'video_validation_limit')
        with patch('core.scanner.video.validation.MAX_BOXES', 2):
            self.assertEqual(self.check(movie()).integrity, 'video_validation_limit')

    def test_deterministic_metadata_mutations_never_raise_unhandled_exceptions(self):
        import random
        data = movie(); end = data.index(b'mdat') - 4
        rng = random.Random(8)
        for _ in range(400):
            mutated = bytearray(data)
            at = rng.randrange(end); mutated[at] ^= rng.randrange(1, 256)
            result = self.check(mutated)
            self.assertTrue(result.integrity.startswith('video_'))
            self.assertLessEqual(result.size, len(data))


class VideoSignatureIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.path = self.root / 'source.img'
        self.config = AppConfig(self.root / 'work'); self.config.initialize()
        self.repo = Repository(self.config.database)
        self.video = files('app').joinpath('assets/preview-test.mp4').read_bytes()

    def scan(self, data, **options):
        self.path.write_bytes(data)
        return list(SignatureScanner(**options).scan(self.path, CancellationToken()))

    def persist(self, candidates, computer=False):
        source = DataSource('computer' if computer else 'image', str(self.path))
        task = self.repo.create_task('computer_scan' if computer else 'signature_scan', source)
        for item in candidates: self.repo.add_candidate(task, replace(item, source_id=source.id))
        self.repo.update_task(task, 'completed')
        return task

    def test_ftyp_crosses_every_block_boundary_once_with_correct_start(self):
        data = movie(mov=True)
        for offset in range(8, 24):
            with self.subTest(offset=offset):
                items = self.scan(b'x' * offset + data + b'trailer', block_size=8)
                self.assertEqual(len(items), 1)
                item = items[0]
                self.assertEqual((item.offset, item.size, item.file_type, item.integrity), (offset, len(data), 'mov', 'video_structure_checked'))
                self.assertEqual(item.sha256, hashlib.sha256(data).hexdigest())
                self.assertEqual(item.deletion_state, 'unknown')

    def test_checked_video_suppresses_nested_image_but_rejected_one_does_not(self):
        png = png_image()
        data = movie(payload=png + b'pad')
        self.assertEqual(len(self.scan(data)), 1)
        bad = data.replace(b'avc1', b'hvc1')
        items = self.scan(bad + b'gap' + self.video)
        self.assertEqual([item.file_type for item in items], ['mp4', 'png', 'mp4'])
        self.assertEqual(items[-1].integrity, 'video_structure_checked')

    def test_read_only_scan_pagination_preview_export_and_history(self):
        original = b'prefix' + self.video + b'gap' + movie(mov=True) + b'trailer'
        self.path.write_bytes(original); before = self.path.stat()
        original_open = Path.open; modes = []
        def watched(path, mode='r', *args, **kwargs):
            if path.resolve() == self.path: modes.append(mode)
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', watched):
            items = list(SignatureScanner(block_size=97).scan(self.path, CancellationToken()))
        self.assertEqual(modes, ['rb', 'rb'])
        task = self.persist(items)
        self.assertEqual(self.repo.candidate_count(task, category='video'), 2)
        first = self.repo.candidate_page(task, category='video', limit=1)[0]
        second = self.repo.candidate_page(task, category='video', offset=1, limit=1)[0]
        self.assertEqual((first['offset'], second['file_type']), (6, 'mov'))
        preview = prepare_video(self.repo, first['id'], self.config.data_dir, CancellationToken())
        self.assertEqual((preview.width, preview.height, preview.data), (96, 64, self.video))
        export = self.repo.create_task('export')
        export_batch(self.repo, export, [first['id']], self.root / 'output', self.config.data_dir, CancellationToken())
        self.repo.update_task(export, 'completed')
        record = self.repo.exports(export)[0]
        self.assertEqual(record['status'], 'exported')
        self.assertEqual(record['source_sha256'], record['target_sha256'])
        self.assertEqual(Path(record['output_path']).read_bytes(), self.video)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.path.unlink()
        reopened = Repository(self.config.database)
        self.assertEqual(reopened.candidate_page(task, category='video', limit=1), [first])
        self.assertEqual(reopened.exports(export)[0], record)
        with self.assertRaises(CandidateAccessError):
            prepare_video(reopened, first['id'], self.config.data_dir, CancellationToken())

    def test_partial_candidates_have_no_digest_and_cannot_export(self):
        for data in (self.video[:-1], movie().replace(b'avc1', b'encv')):
            items = self.scan(data); item = items[0]
            self.assertEqual(item.status, 'partial'); self.assertIsNone(item.sha256)
            task = self.persist(items)
            with self.assertRaises(CandidateAccessError): candidate_input(self.repo, item.id, self.config.data_dir)

    def test_changed_source_is_rejected_before_preview(self):
        item = self.scan(self.video)[0]; self.persist([item])
        data = bytearray(self.video); data[-1] ^= 1; self.path.write_bytes(data)
        with self.assertRaises(CandidateAccessError): prepare_video(self.repo, item.id, self.config.data_dir, CancellationToken())

    def test_computer_scan_uses_same_ranges_digest_and_preview_path(self):
        original = b'prefix' + self.video + b'padding'; self.path.write_bytes(original)
        items = list(ComputerScanner(self.config.data_dir).scan(self.path, CancellationToken(), mode='signature'))
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item.offset, item.extents, item.sha256, item.integrity), (6, ((6, len(self.video)),), hashlib.sha256(self.video).hexdigest(), 'video_structure_checked'))
        self.persist(items, computer=True)
        self.assertEqual(prepare_video(self.repo, item.id, self.config.data_dir, CancellationToken()).data, self.video)
        self.assertEqual(self.path.read_bytes(), original)

    def test_computer_partition_bounds_and_absolute_offsets(self):
        self.path.write_bytes(b'x' * 512 + self.video + movie())
        with DiskReader(self.path) as reader:
            partition = Partition(512, len(self.video), 'APFS', 'synthetic')
            items = list(ComputerScanner()._signatures(reader, partition, CancellationToken()))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['extents'], [(512, len(self.video))])

    def test_candidate_cap_and_mid_scan_cancellation(self):
        self.path.write_bytes(self.video * 2)
        with self.assertRaises(ScanError): list(SignatureScanner(max_candidates=1).scan(self.path, CancellationToken()))
        token = CancellationToken(); scanner = SignatureScanner().scan(self.path, token)
        next(scanner); token.cancel()
        with self.assertRaises(TaskCancelled): next(scanner)

    def test_real_mov_with_tail_metadata_prepares_from_exact_scanned_range(self):
        data = mov_fixture(self.video)
        self.assertGreater(data.index(b'moov'), data.index(b'mdat'))
        item = self.scan(b'prefix' + data + b'trailer')[0]
        self.assertEqual((item.file_type, item.size, item.integrity), ('mov', len(data), 'video_structure_checked'))
        self.persist([item])
        preview = prepare_video(self.repo, item.id, self.config.data_dir, CancellationToken())
        self.assertEqual(preview.data, data)

    def test_partial_computer_candidate_preserves_offset_without_exportable_extents(self):
        self.path.write_bytes(b'prefix' + self.video[:-1])
        item = list(ComputerScanner(self.config.data_dir).scan(self.path, CancellationToken(), mode='signature'))[0]
        self.assertEqual((item.offset, item.status, item.extents, item.sha256), (6, 'partial', (), None))

    def test_explanations_distinguish_range_check_deletion_and_bitstream(self):
        item = self.scan(self.video)[0]
        candidate = {'integrity': item.integrity, 'source': item.source, 'structure_details': item.structure_details}
        self.assertIn('采样范围', content_check(item.integrity).label)
        self.assertIn('未逐帧解码', video_check_details(candidate))
        self.assertIn('未知', deletion_evidence(candidate).label)
        candidate['structure_details'] = {'check_version': '<script>'}
        self.assertEqual(video_check_details(candidate), '')
