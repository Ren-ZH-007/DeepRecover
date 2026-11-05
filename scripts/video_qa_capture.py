"""Real local video playback and rejection workflows on generated test media."""
import argparse
from datetime import datetime, timezone
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import struct
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app.app_config import AppConfig
from app.main_window import MainWindow
from core.candidate_io import CandidateAccessError, snapshot
from storage.database.repository import Repository
from storage.models.candidate import Candidate
from storage.models.source import DataSource
from scripts.computer_qa_capture import verify_text_glyphs


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([]); app.setQuitOnLastWindowClosed(False)
    cases, shots = [], []
    fixture = files('app').joinpath('assets/preview-test.mp4').read_bytes()
    with tempfile.TemporaryDirectory(prefix='DeepRecover-Video-QA-') as temporary:
        root = Path(temporary).resolve(); config = AppConfig(root / 'workspace'); config.initialize()
        repo = Repository(config.database)
        source = DataSource('android', 'synthetic-video-device')
        task = repo.create_task('android_acquisition', source)
        clip = config.data_dir / 'Acquisitions/synthetic-clip'; clip.write_bytes(fixture)
        candidate = Candidate('android_file', '合成测试视频.mp4', 'mp4', len(fixture),
            source_id=source.id, local_path=str(clip), sha256=hashlib.sha256(fixture).hexdigest(),
            source_stat=snapshot(clip.stat()), integrity='not_checked')
        repo.add_candidate(task, candidate); repo.update_task(task, 'completed')
        original_candidate = repo.candidate(candidate.id)
        original_hash = hashlib.sha256(clip.read_bytes()).hexdigest()
        window = MainWindow(config, repo); window.timer.stop(); window.show()
        window.open_results(task); app.processEvents()
        font_check = verify_text_glyphs(window.font())

        def record(identifier, **values): cases.append({'id': identifier, 'result': 'passed', **values})

        def capture(name, widget):
            QTest.qWait(40); app.processEvents()
            assert widget.grab().save(str(output / name), 'PNG')
            shots.append(name)

        def wait_until(predicate):
            for _ in range(500):
                if predicate(): return
                QTest.qWait(20)
            raise AssertionError('Video verification timed out')

        def open_video(row=0):
            window.result_table.selectRow(row); window.preview_selected()
            assert window.pending is not None
            window.pending[0].future.result(15); window.poll()
            dialog = window.video_dialog; assert dialog is not None
            wait_until(lambda: dialog.frame_count > 0 or dialog._failed)
            assert not dialog._failed and dialog.frame_count > 0
            return dialog

        try:
            dialog = open_video()
            assert not dialog.canvas.pixmap().isNull()
            assert (dialog._image.width(), dialog._image.height()) == (96, 64)
            assert dialog.player.audioOutput() is None and dialog.player.hasAudio()
            assert dialog.player.source().isEmpty() and dialog.player.sourceDevice() is dialog.buffer
            capture('01-actual-video-first-frame.png', dialog)
            record('VQA-01', actual_frame_decoded=True, source_url_empty=True, memory_device_only=True,
                audio_track_present=True, no_audio_output=True)

            dialog.play_button.click()
            wait_until(lambda: dialog.frame_count >= 2 and dialog.player.position() >= 125)
            dialog.play_button.click()
            assert dialog.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
            frame_count = dialog.frame_count
            dialog.seek_slider.setValue(1000); dialog.seek()
            wait_until(lambda: dialog.player.position() >= 900 and dialog.frame_count > frame_count)
            capture('02-playback-pause-and-seek.png', dialog)
            record('VQA-02', actual_playback_advanced=True, pause_verified=True, seek_frame_decoded=True)

            window.result_table.clearSelection()
            assert dialog._closed and dialog.buffer.size() == 0 and not dialog.sink.videoFrame().isValid()
            assert window.video_dialog is None
            capture('03-selection-change-releases-video.png', window)
            record('VQA-03', selection_stops_player=True, compressed_buffer_released=True, decoded_frame_released=True)

            # Change the self-contained data reference flag of the synthetic MP4.
            invalid_data = bytearray(fixture); index = invalid_data.index(b'url ') + 4
            invalid_data[index:index + 4] = b'\0\0\0\0'
            invalid_path = config.data_dir / 'Acquisitions/referenced-clip'; invalid_path.write_bytes(invalid_data)
            invalid = Candidate('android_file', '外部引用拒绝.mp4', 'mp4', len(invalid_data),
                source_id=source.id, local_path=str(invalid_path), source_stat=snapshot(invalid_path.stat()),
                sha256=hashlib.sha256(invalid_data).hexdigest(), integrity='not_checked')
            repo.add_candidate(task, invalid); window.open_results(task); window.result_table.selectRow(1)
            with patch('ui.preview.video.QMediaPlayer', side_effect=AssertionError('Rejected media entered decoder')) as backend:
                window.preview_selected(); assert window.pending is not None
                try: window.pending[0].future.result(15)
                except CandidateAccessError: pass
                else: raise AssertionError('External reference was accepted')
                window.poll(); backend.assert_not_called()
            assert window.video_dialog is None and '外部媒体引用' in window.preview_label.text()
            capture('04-external-reference-rejected.png', window)
            record('VQA-04', external_reference_refused_before_decoder=True, no_backend_started=True)

            window.open_results(task); dialog = open_video()
            dialog.player.errorOccurred.emit(QMediaPlayer.Error.FormatError, '/private/user/DO-NOT-SHOW.mp4')
            assert dialog._failed and dialog.buffer.size() == 0 and not dialog.play_button.isEnabled()
            assert 'private' not in dialog.status.text() and 'DO-NOT-SHOW' not in dialog.status.text()
            capture('05-decoder-error-clears-frame.png', dialog)
            record('VQA-05', injected_decoder_error_redacted=True, stale_frame_cleared=True, buffer_released=True)
            dialog.close()

            window.open_results(task)
            with patch('app.main_window.prepare_video', side_effect=AssertionError('History read source content')):
                window.open_results(task)
                assert window.result_table.rowCount() == 2
            assert repo.candidate(candidate.id) == original_candidate
            assert hashlib.sha256(clip.read_bytes()).hexdigest() == original_hash
            assert invalid_path.read_bytes() == bytes(invalid_data)
            assert list((config.data_dir / 'ScanCache').iterdir()) == []
            assert list((config.data_dir / 'Recovered').iterdir()) == []
            record('VQA-06', history_does_not_prepare_video=True, candidate_unchanged=True,
                sources_unchanged=True, no_preview_files=True, no_export_files=True)

            rotated = bytearray(fixture); body = rotated.index(b'tkhd') + 4
            assert rotated[body] == 0
            struct.pack_into('>9i', rotated, body + 40, 0, -65536, 0, 65536, 0, 0, 0, 0, 1073741824)
            portrait_path = config.data_dir / 'Acquisitions/portrait-clip'; portrait_path.write_bytes(rotated)
            portrait = Candidate('android_file', '手机竖屏旋转.mp4', 'mp4', len(rotated), source_id=source.id,
                local_path=str(portrait_path), source_stat=snapshot(portrait_path.stat()),
                sha256=hashlib.sha256(rotated).hexdigest(), integrity='not_checked')
            repo.add_candidate(task, portrait); window.open_results(task)
            dialog = open_video(2)
            assert (dialog._image.width(), dialog._image.height()) == (64, 96)
            capture('06-phone-portrait-rotation.png', dialog)
            dialog.close()
            assert portrait_path.read_bytes() == bytes(rotated)
            assert repo.candidate(portrait.id)['status'] == 'candidate'
            record('VQA-07', rotation_metadata_applied=True, portrait_frame_decoded=True,
                portrait_source_and_candidate_unchanged=True)
        finally:
            window.close(); app.processEvents()

    evidence = {'date_utc': datetime.now(timezone.utc).date().isoformat(), 'platform': sys.platform,
        'architecture': platform.machine(), 'qt_platform': app.platformName(),
        'cases': cases, 'screenshot_font_check': font_check, 'source_hashes_unchanged': True,
        'candidate_statuses_unchanged': True, 'preview_files_created': False,
        'raw_devices_opened': False, 'network_media_sources_opened': False,
        'screenshots': [{'file': name, 'sha256': hashlib.sha256((output / name).read_bytes()).hexdigest()} for name in shots]}
    (output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'workflow_cases': len(cases), 'passed': len(cases), 'screenshots': len(shots)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output.resolve())
