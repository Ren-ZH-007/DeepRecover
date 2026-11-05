"""Synthetic media, source boundaries and the real Qt in-memory playback path."""
from dataclasses import replace
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from app.app_config import AppConfig
from core.candidate_io import CandidateAccessError, snapshot
from core.computer.io import DiskReader
from core.preview.video import inspect_video, prepare_video
from core.tasks import CancellationToken, TaskCancelled
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from storage.models.source import DataSource


def video_fixture():
    return files('app').joinpath('assets/preview-test.mp4').read_bytes()


def rotated_video_fixture():
    data = bytearray(video_fixture())
    body = data.index(b'tkhd') + 4
    assert data[body] == 0
    # Standard 90-degree QuickTime track display matrix; media bytes unchanged.
    struct.pack_into('>9i', data, body + 40, 0, -65536, 0, 65536, 0, 0, 0, 0, 1073741824)
    return bytes(data)


def atom(kind, payload):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def container(*, url_flags=1, uri=b'', codec=b'avc1', width=96, height=64, duration=2000, timescale=1000):
    """Minimal admission fixture; not used to claim successful media decoding."""
    clock = b'\0' * 12 + struct.pack('>II', timescale, duration)
    visual = bytearray(78); struct.pack_into('>H', visual, 6, 1)
    struct.pack_into('>HH', visual, 24, width, height)
    sample = atom(codec, visual + atom(b'avcC', b'\1\x64\0\x0a\xff\xe1\0'))
    stsd = atom(b'stsd', b'\0' * 4 + struct.pack('>I', 1) + sample)
    stts = atom(b'stts', b'\0' * 4 + struct.pack('>III', 1, 16, 125))
    dref = atom(b'dref', b'\0' * 4 + struct.pack('>I', 1) + atom(b'url ', struct.pack('>I', url_flags) + uri))
    minf = atom(b'minf', atom(b'dinf', dref) + atom(b'stbl', stsd + stts))
    media = atom(b'mdia', atom(b'mdhd', clock) + atom(b'hdlr', b'\0' * 8 + b'vide') + minf)
    return atom(b'ftyp', b'isom\0\0\0\0isom') + atom(b'moov', atom(b'mvhd', clock) + atom(b'trak', media)) + atom(b'mdat', b'fixture bytes')


class VideoPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = AppConfig(self.root / 'workspace'); self.config.initialize()
        self.repo = Repository(self.config.database)

    def android(self, *, data=None, extension='mp4', digest=None):
        data = video_fixture() if data is None else data
        path = self.config.data_dir / 'Acquisitions' / 'collected-data'; path.write_bytes(data)
        source = DataSource('android', 'test-serial')
        task = self.repo.create_task('android_acquisition', source)
        candidate = Candidate('android_file', '测试视频.' + extension, extension, len(data),
            local_path=str(path), source_id=source.id, source_stat=snapshot(path.stat()),
            sha256=digest or hashlib.sha256(data).hexdigest(), integrity='not_checked')
        self.repo.add_candidate(task, candidate); self.repo.update_task(task, 'completed')
        return task, candidate, path

    def test_real_fixture_admission_dimensions_and_duration(self):
        data = video_fixture(); result = inspect_video(data, CancellationToken())
        self.assertEqual((result.width, result.height, result.duration_ms), (96, 64, 2000))
        self.assertIs(result.data, data)

    def test_android_preparation_preserves_source_candidate_database_and_no_cache_files(self):
        task, candidate, path = self.android()
        before_db = self.config.database.read_bytes()
        before_files = sorted(p.relative_to(self.config.data_dir) for p in self.config.data_dir.rglob('*'))
        result = prepare_video(self.repo, candidate.id, self.config.data_dir, CancellationToken())
        self.assertEqual(result.data, path.read_bytes())
        self.assertEqual(self.config.database.read_bytes(), before_db)
        self.assertEqual(self.repo.candidate(candidate.id)['integrity'], 'not_checked')
        self.assertEqual(self.repo.candidate(candidate.id)['status'], 'candidate')
        self.assertEqual(sorted(p.relative_to(self.config.data_dir) for p in self.config.data_dir.rglob('*')), before_files)

    def test_ios_blob_with_no_extension_is_read_only_and_bound_to_backup(self):
        root = self.root / 'backup'; root.mkdir(); path = root / '0123456789abcdef'; path.write_bytes(video_fixture())
        source = DataSource('ios_backup', str(root)); task = self.repo.create_task('ios_manifest', source)
        candidate = Candidate('ios_backup', 'Camera/video.mov', 'mov', path.stat().st_size,
            source_id=source.id, local_path=str(path), source_stat=snapshot(path.stat()), integrity='not_checked')
        self.repo.add_candidate(task, candidate); self.repo.update_task(task, 'completed')
        before = path.read_bytes()
        self.assertEqual(prepare_video(self.repo, candidate.id, self.config.data_dir, CancellationToken()).data, before)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(root.iterdir()), [path])

    def test_fragmented_computer_image_preserves_logical_order_and_source(self):
        data = video_fixture(); path = self.root / 'fragmented.img'
        split = 4096
        # Logical first part is physically after the second part.
        raw = b'prefix' + data[split:] + b'gap' + data[:split]; path.write_bytes(raw)
        source = DataSource('computer', str(path)); task = self.repo.create_task('computer_scan', source)
        with DiskReader(path) as reader: identity = reader.check()
        extents = ((6 + len(data) - split + 3, split), (6, len(data) - split))
        candidate = Candidate('computer_deleted', 'deleted.mp4', 'mp4', len(data), source_id=source.id,
            source_stat=identity, extents=extents, sha256=hashlib.sha256(data).hexdigest(),
            integrity='metadata_checked', deletion_state='deleted_record')
        self.repo.add_candidate(task, candidate); self.repo.update_task(task, 'completed')
        self.assertEqual(prepare_video(self.repo, candidate.id, self.config.data_dir, CancellationToken()).data, data)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.repo.candidate(candidate.id)['status'], 'candidate')

    def test_changed_source_or_digest_is_rejected_before_playback(self):
        _, candidate, path = self.android(digest='0' * 64)
        with self.assertRaisesRegex(CandidateAccessError, 'SHA-256'):
            prepare_video(self.repo, candidate.id, self.config.data_dir, CancellationToken())
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaises(CandidateAccessError):
            prepare_video(self.repo, candidate.id, self.config.data_dir, CancellationToken())

    def test_input_cap_rejects_without_reading_content(self):
        _, candidate, _ = self.android()
        oversized = replace(candidate, id='oversized-video', size=64 * 1024 * 1024 + 1)
        self.repo.add_candidate(self.repo.candidate(candidate.id)['task_id'], oversized)
        with patch('core.preview.video.read_candidate') as read:
            with self.assertRaisesRegex(CandidateAccessError, '64 MiB'):
                prepare_video(self.repo, oversized.id, self.config.data_dir, CancellationToken())
        read.assert_not_called()

    def test_running_task_missing_source_or_outside_acquisitions_cannot_be_previewed(self):
        task, candidate, _ = self.android()
        self.repo.update_task(task, 'running')
        with self.assertRaises(CandidateAccessError): prepare_video(self.repo, candidate.id, self.config.data_dir, CancellationToken())
        self.repo.update_task(task, 'completed')
        outside = self.root / 'outside'; outside.write_bytes(video_fixture())
        invalid = replace(candidate, id='outside-video', local_path=str(outside), source_stat=snapshot(outside.stat()))
        self.repo.add_candidate(task, invalid)
        with self.assertRaises(CandidateAccessError): prepare_video(self.repo, invalid.id, self.config.data_dir, CancellationToken())

    def test_unsupported_extensions_and_playlists_are_not_admitted(self):
        for extension in ('avi', 'mkv', 'm3u8'):
            _, candidate, _ = self.android(extension=extension)
            with self.subTest(extension=extension), self.assertRaisesRegex(CandidateAccessError, 'MP4/MOV'):
                prepare_video(self.repo, candidate.id, self.config.data_dir, CancellationToken())
        with self.assertRaises(CandidateAccessError): inspect_video(b'#EXTM3U\nhttps://example.invalid/video', CancellationToken())

    def test_external_url_or_local_file_reference_is_rejected_without_echoing_locator(self):
        for uri in (b'https://example.invalid/private.mp4\0', b'file:///private/source.mov\0'):
            with self.subTest(uri=uri), self.assertRaises(CandidateAccessError) as error:
                inspect_video(container(url_flags=0, uri=uri), CancellationToken())
            self.assertIn('外部媒体引用', str(error.exception))
            self.assertNotIn('private', str(error.exception))

    def test_reference_movies_fragmented_streams_and_encryption_are_rejected(self):
        for extra in (atom(b'rmra', b'reference'), atom(b'moof', b'fragment'), atom(b'pssh', b'protected')):
            with self.subTest(extra=extra), self.assertRaises(CandidateAccessError):
                inspect_video(container() + extra, CancellationToken())
        for codec in (b'hvc1', b'avc3', b'encv'):
            with self.subTest(codec=codec), self.assertRaises(CandidateAccessError):
                inspect_video(container(codec=codec), CancellationToken())

    def test_active_reference_hidden_in_user_metadata_is_rejected(self):
        data = container()
        start = data.index(b'moov') - 4
        size = struct.unpack_from('>I', data, start)[0]
        hidden = atom(b'udta', atom(b'rmra', atom(b'rdrf', b'file:///private/source.mov')))
        changed = data[:start] + struct.pack('>I', size + len(hidden)) + data[start + 4:start + size] + hidden + data[start + size:]
        with self.assertRaises(CandidateAccessError) as error:
            inspect_video(changed, CancellationToken())
        self.assertNotIn('private', str(error.exception))

    def test_active_box_hidden_in_audio_sample_description_is_rejected(self):
        data = bytearray(video_fixture())
        index = data.index(b'esds', data.index(b'mp4a'))
        data[index:index + 4] = b'dref'
        with self.assertRaises(CandidateAccessError):
            inspect_video(bytes(data), CancellationToken())

    def test_truncation_invalid_sizes_duplicate_headers_and_unknown_boxes_fail_closed(self):
        data = container()
        for invalid in (data[:-1], b'\0\0\0\1mdat', data + atom(b'ftyp', b'isom\0\0\0\0'), data + atom(b'junk', b'x')):
            with self.subTest(invalid=invalid[:16]), self.assertRaises(CandidateAccessError):
                inspect_video(invalid, CancellationToken())

    def test_declared_dimensions_duration_and_frame_rate_are_bounded(self):
        for values in ({'width': 4096}, {'height': 2160}, {'duration': 600001}, {'timescale': 0}, {'timescale': 100000}):
            with self.subTest(values=values), self.assertRaises(CandidateAccessError):
                inspect_video(container(**values), CancellationToken())

    def test_1080p_limit_allows_portrait_orientation(self):
        preview = inspect_video(container(width=1080, height=1920), CancellationToken())
        self.assertEqual((preview.width, preview.height), (1080, 1920))

    def test_box_budget_and_cancellation_during_container_walk(self):
        token = CancellationToken(); token.cancel()
        with self.assertRaises(TaskCancelled): inspect_video(video_fixture(), token)
        with patch('core.preview.video.MAX_BOXES', 2), self.assertRaises(CandidateAccessError):
            inspect_video(container(), CancellationToken())


os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QTimer
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from app.main_window import MainWindow
    from ui.preview.video import VideoPreviewDialog
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, 'PySide6 not installed')
class VideoPreviewUiTests(unittest.TestCase):
    android = VideoPreparationTests.android

    def setUp(self):
        VideoPreparationTests.setUp(self)
        self.app = QApplication.instance() or QApplication([])
        self.window = MainWindow(self.config, self.repo); self.window.timer.stop()
        self.addCleanup(self.window.close)

    def finish(self):
        handle = self.window.pending[0]
        handle.future.result(5); self.window.poll(); self.app.processEvents()

    def wait_frame(self, dialog):
        for _ in range(160):
            if dialog.frame_count or dialog._failed: break
            QTest.qWait(25)
        self.assertFalse(dialog._failed, dialog.status.text())
        self.assertGreater(dialog.frame_count, 0, 'Actual video frame was not decoded')

    def opened(self):
        task, candidate, path = self.android()
        self.window.open_results(task); self.window.result_table.selectRow(0)
        self.window.preview_selected(); self.finish()
        dialog = self.window.video_dialog; self.assertIsNotNone(dialog)
        self.wait_frame(dialog)
        return task, candidate, path, dialog

    def test_actual_video_frame_pause_play_seek_and_no_source_file_in_media_backend(self):
        task, candidate, path, dialog = self.opened()
        before = path.read_bytes()
        self.assertTrue(dialog.player.source().isEmpty())
        self.assertIs(dialog.player.sourceDevice(), dialog.buffer)
        self.assertIsNone(dialog.player.audioOutput())
        self.assertTrue(dialog.player.hasAudio())
        self.assertFalse(dialog.canvas.pixmap().isNull())
        self.assertEqual(dialog.player.playbackState(), QMediaPlayer.PlaybackState.PausedState)
        dialog.play_button.click(); QTest.qWait(80)
        self.assertEqual(dialog.player.playbackState(), QMediaPlayer.PlaybackState.PlayingState)
        dialog.play_button.click()
        dialog.seek_slider.setValue(1000); dialog.seek(); QTest.qWait(100)
        self.assertGreaterEqual(dialog.player.position(), 900)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.repo.candidate(candidate.id)['status'], 'candidate')
        self.assertEqual([item['id'] for item in self.repo.tasks()], [task])
        dialog.close()
        self.assertFalse(dialog.buffer.isOpen()); self.assertEqual(dialog.buffer.size(), 0)
        self.assertFalse(dialog.sink.videoFrame().isValid())

    def test_change_selection_filter_page_or_navigation_stops_playback(self):
        task, _, _, dialog = self.opened()
        self.window.result_table.clearSelection(); self.assertTrue(dialog._closed)
        self.assertIsNone(self.window.video_dialog)
        self.window.result_table.selectRow(0); self.window.preview_selected(); self.finish()
        dialog = self.window.video_dialog
        self.window.category_filter.setCurrentIndex(self.window.category_filter.findData('image'))
        self.assertTrue(dialog._closed); self.assertIsNone(self.window.video_dialog)
        self.window.category_filter.setCurrentIndex(self.window.category_filter.findData('all'))
        self.window.result_table.selectRow(0); self.window.preview_selected(); self.finish()
        dialog = self.window.video_dialog
        self.window.nav.setCurrentRow(6)
        self.assertTrue(dialog._closed); self.assertIsNone(self.window.video_dialog)

    def test_selection_changed_during_preparation_discards_late_result(self):
        task, _, _ = self.android(); self.window.open_results(task)
        self.window.result_table.selectRow(0); self.window.preview_selected()
        self.window.result_table.clearSelection(); self.finish()
        self.assertIsNone(self.window.video_dialog)

    def test_decode_error_and_timeout_release_buffer_and_do_not_show_private_backend_text(self):
        preview = inspect_video(video_fixture(), CancellationToken())
        dialog = VideoPreviewDialog(preview, self.window); self.addCleanup(dialog.close)
        dialog.player.errorOccurred.emit(QMediaPlayer.Error.FormatError, '/private/user/source.mov')
        self.assertTrue(dialog._failed); self.assertEqual(dialog.buffer.size(), 0)
        self.assertNotIn('private', dialog.status.text())
        self.assertFalse(dialog.play_button.isEnabled())
        self.assertTrue(dialog.canvas.pixmap().isNull())
        second = VideoPreviewDialog(preview, self.window); self.addCleanup(second.close)
        second.load_timer.timeout.emit()
        self.assertTrue(second._failed); self.assertIn('超时', second.status.text())
        self.assertEqual(second.buffer.size(), 0)

    def test_real_incomplete_media_decoder_fails_and_releases_memory(self):
        # Header admission is deliberately distinct from successful decoding.
        preview = inspect_video(container(), CancellationToken())
        dialog = VideoPreviewDialog(preview, self.window); self.addCleanup(dialog.close)
        if not dialog._failed:
            dialog.load_timer.start(500)
        for _ in range(80):
            if dialog._failed: break
            QTest.qWait(25)
        self.assertTrue(dialog._failed)
        self.assertEqual(dialog.buffer.size(), 0)
        self.assertTrue(dialog.canvas.pixmap().isNull())
        self.assertFalse(dialog.play_button.isEnabled())

    def test_main_window_close_releases_open_video_window(self):
        _, _, _, dialog = self.opened()
        self.window.close()
        self.assertTrue(dialog._closed); self.assertFalse(dialog.isVisible())

    def test_phone_rotation_metadata_is_applied_to_actual_preview_frame(self):
        task, _, _ = self.android(data=rotated_video_fixture())
        self.window.open_results(task); self.window.result_table.selectRow(0)
        self.window.preview_selected(); self.finish()
        dialog = self.window.video_dialog; self.wait_frame(dialog)
        self.assertEqual((dialog._image.width(), dialog._image.height()), (64, 96))

    def test_pagination_or_starting_another_task_closes_the_video(self):
        task, _, _, dialog = self.opened()
        self.repo.add_candidates(task, [Candidate('test', str(i), 'txt', 0) for i in range(101)])
        self.window.change_result_page(1)
        self.assertTrue(dialog._closed); self.assertIsNone(self.window.video_dialog)
        self.window.change_result_page(-1)
        self.window.result_table.selectRow(0); self.window.preview_selected(); self.finish()
        dialog = self.window.video_dialog
        self.window.run_task('other_work', lambda token: True, lambda *_: None)
        self.assertTrue(dialog._closed); self.assertIsNone(self.window.video_dialog)
        self.finish()

    def test_original_raw_disk_preflight_still_precedes_any_preview_task(self):
        task, _, _ = self.android(); self.window.open_results(task); self.window.result_table.selectRow(0)
        before = self.repo.tasks()
        from core.candidate_io import CandidateAccessError
        with patch.object(self.window, 'requires_computer_preflight', return_value=True), \
                patch('app.main_window.check_computer_access', side_effect=CandidateAccessError('same physical source')):
            self.window.preview_selected(); future = self.window.computer_page._future
            with self.assertRaises(CandidateAccessError): future.result(5)
            self.window.poll()
        self.assertIsNone(self.window.pending); self.assertIsNone(self.window.video_dialog)
        self.assertEqual(self.repo.tasks(), before)
