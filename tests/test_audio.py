"""WAV, MP3 and M4A signature candidates: structure checks, scanning, export and resume."""
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import random
import struct
import tempfile
from types import SimpleNamespace
import unittest

from app.app_config import AppConfig
from core.candidate_io import read_candidate
from core.computer.service import ComputerScanner
from core.export.service import ExportError, export_batch
from core.scanner.audio.validation import AUDIO_TYPES, KSDATAFORMAT_TAIL
from core.scanner.resume import plan_resume, resume_operation, verify_resume
from core.scanner.signature.scanner import SignatureScanner
from core.scanner.validation import CHECKED, INVALID, checked_signature, validate
from core.tasks import CancellationToken, TaskCancelled, TaskManager
from storage.database.repository import Repository
from storage.models.classification import category
from storage.models.source import DataSource
from tests.test_resume import interrupt_after, summary
from tests.video_signature_fixture import movie
from ui.recovery.presentation import audio_check_details, content_check

FIXTURES = Path(__file__).parent / 'fixtures' / 'audio'
TOKEN = CancellationToken()
KIND = {'.wav': 'wav', '.mp3': 'mp3', '.m4a': 'mp4'}


def check(blob, kind, *, before=b'', after=b'', max_size=64 << 20):
    data = before + blob + after
    return validate(io.BytesIO(data), len(before), len(data), kind, TOKEN, max_size, 256 << 20)


def fixture(name):
    return (FIXTURES / name).read_bytes()


def wav(*, code=1, channels=1, rate=8000, bits=16, frames=100, chunks=None, riff=None, align=None, byte_rate=None,
        extensible=None, form=b'WAVE'):
    align = channels * bits // 8 if align is None else align
    byte_rate = rate * align if byte_rate is None else byte_rate
    fmt = struct.pack('<HHIIHH', 0xFFFE if extensible else code, channels, rate, byte_rate, align, bits)
    if extensible:
        fmt += struct.pack('<HHI', 22, bits, 0) + struct.pack('<H', extensible) + KSDATAFORMAT_TAIL
    parts = chunks if chunks is not None else [(b'fmt ', fmt), (b'data', b'\x01' * (align * frames))]
    body = b''.join(kind + struct.pack('<I', len(data)) + data + b'\0' * (len(data) & 1) for kind, data in parts)
    return b'RIFF' + struct.pack('<I', 4 + len(body) if riff is None else riff) + form + body


class WavTests(unittest.TestCase):
    def test_real_files_have_exact_boundaries(self):
        for name, encoding, channels in (('tone_pcm16.wav', 'pcm', 1), ('tone_float.wav', 'float', 2)):
            with self.subTest(name=name):
                blob = fixture(name)
                result = check(blob, 'wav', before=b'\x07' * 31, after=b'\x08' * 500)
                self.assertEqual((result.integrity, result.file_type, result.size), ('audio_structure_checked', 'wav', len(blob)))
                self.assertEqual((result.details['encoding'], result.details['channels'], result.details['sample_rate']),
                                 (encoding, channels, 8000))

    def test_supported_layouts(self):
        fmt = wav()[20:36]
        cases = {
            'pcm 24-bit stereo': wav(channels=2, bits=24), 'float 64': wav(code=3, bits=64), 'a-law': wav(code=6, bits=8),
            'mu-law': wav(code=7, bits=8), 'extensible pcm': wav(extensible=1, channels=6), 'extensible float': wav(extensible=3, bits=32),
            'extra chunks around data': wav(chunks=[(b'LIST', b'INFOISFTx\0\0\0'), (b'fmt ', fmt), (b'fact', b'\0' * 4),
                                                    (b'data', b'\x02' * 200), (b'id3 ', b'ID3tag')]),
            'odd chunk with pad byte': wav(chunks=[(b'fmt ', fmt), (b'data', b'\x02' * 200), (b'note', b'odd')]),
        }
        for label, blob in cases.items():
            with self.subTest(label=label):
                result = check(blob, 'wav', after=b'\xaa' * 40)
                self.assertEqual((result.integrity, result.size), ('audio_structure_checked', len(blob)))
        # A final odd chunk whose pad byte the RIFF size does not count still includes the pad.
        short = wav(chunks=[(b'fmt ', fmt), (b'data', b'\x02' * 200), (b'note', b'odd')])
        short = short[:4] + struct.pack('<I', len(short) - 9) + short[8:]
        self.assertEqual(check(short, 'wav', after=b'\xaa' * 4).size, len(short))

    def test_other_riff_forms_are_not_candidates(self):
        for form in (b'AVI ', b'WEBP', b'RMID'):
            self.assertIsNone(check(wav(form=form), 'wav'))
        self.assertIsNone(check(b'RIFF\x10\0', 'wav'))

    def test_inconsistent_or_unsupported_wavs(self):
        fmt = wav()[20:36]
        invalid = {
            'block align': wav(align=3), 'byte rate': wav(byte_rate=1), 'bits': wav(bits=12),
            'data before fmt': wav(chunks=[(b'data', b'\x02' * 200), (b'fmt ', fmt)]),
            'no data': wav(chunks=[(b'fmt ', fmt), (b'LIST', b'x' * 20)]),
            'two fmt': wav(chunks=[(b'fmt ', fmt), (b'fmt ', fmt), (b'data', b'\x02' * 200)]),
            'partial sample frame': wav(chunks=[(b'fmt ', fmt), (b'data', b'\x02' * 201)]),
            'chunk past riff': wav(riff=40),
        }
        for label, blob in invalid.items():
            with self.subTest(label=label):
                self.assertEqual(check(blob, 'wav').integrity, 'audio_invalid_structure')
        self.assertEqual(check(wav(code=2), 'wav').integrity, 'audio_unsupported')        # ADPCM
        self.assertEqual(check(wav(extensible=0x55), 'wav').integrity, 'audio_unsupported')
        self.assertEqual(check(wav(riff=0), 'wav').integrity, 'audio_boundary_unknown')
        self.assertEqual(check(wav(riff=0xFFFFFFFF), 'wav').integrity, 'audio_boundary_unknown')
        blob = fixture('tone_pcm16.wav')
        self.assertEqual(check(blob[:-10], 'wav').integrity, 'audio_truncated')
        self.assertEqual(check(blob, 'wav', after=b'\0' * 10, max_size=len(blob) - 1).integrity, 'audio_validation_limit')


class Mp3Tests(unittest.TestCase):
    def test_real_files_have_exact_boundaries(self):
        for name, rate, basis in (('tone_44k.mp3', 44100, 'xing_byte_count'), ('tone_cbr.mp3', 8000, 'xing_byte_count'),
                                  ('tone_vbr.mp3', 8000, 'xing_byte_count'), ('tone_id3v1.mp3', 8000, 'xing_byte_count')):
            with self.subTest(name=name):
                blob = fixture(name)
                result = check(blob, 'mp3', before=b'\xff\xfb' * 20, after=b'\xff\xfb\x90\x00' * 50)
                self.assertEqual((result.integrity, result.file_type, result.size), ('audio_structure_checked', 'mp3', len(blob)))
                self.assertEqual((result.details['sample_rate'], result.details['boundary_basis']), (rate, basis))
        self.assertEqual(fixture('tone_id3v1.mp3')[-128:-125], b'TAG')

    def test_missing_byte_count_leaves_the_boundary_unknown(self):
        result = check(fixture('tone_noxing.mp3'), 'mp3')
        self.assertEqual(result.integrity, 'audio_boundary_unknown')
        self.assertFalse(checked_signature(result.integrity, result.file_type))

    def tag_and_frames(self):
        blob = fixture('tone_cbr.mp3')
        raw = blob[6:10]
        end = 10 + (raw[0] << 21 | raw[1] << 14 | raw[2] << 7 | raw[3])
        return blob, end

    def test_damaged_and_unusual_files(self):
        blob, first = self.tag_and_frames()
        self.assertEqual(check(blob[:-200], 'mp3').integrity, 'audio_truncated')
        middle = first + (len(blob) - first) // 2
        frame = blob.find(b'\xff\xe3', middle)          # an MPEG-2.5 Layer III frame header near the middle
        self.assertGreater(frame, 0)
        self.assertEqual(check(blob[:frame] + b'\0\0\0\0' + blob[frame + 4:], 'mp3').integrity, 'audio_invalid_structure')
        info = blob.find(b'Info')
        bigger = blob[:info + 12] + struct.pack('>I', struct.unpack_from('>I', blob, info + 12)[0] + 1000) + blob[info + 16:]
        self.assertIn(check(bigger, 'mp3', after=b'\0' * 2000).integrity, ('audio_invalid_structure', 'audio_truncated'))
        padded = blob[:first] + b'\0' * 100 + blob[first:]
        result = check(padded, 'mp3')
        self.assertEqual((result.integrity, result.size), ('audio_structure_checked', len(padded)))
        self.assertEqual(check(blob[:first] + b'not a frame' + blob[first:], 'mp3').integrity, 'audio_unsupported')

    def test_invalid_tags_are_not_candidates(self):
        blob, _ = self.tag_and_frames()
        for broken in (blob[:3] + b'\x05' + blob[4:], blob[:6] + b'\x80' + blob[7:], b'ID3\x03'):
            self.assertIsNone(check(broken, 'mp3'))


class M4aTests(unittest.TestCase):
    def test_real_files_and_brand_variants(self):
        for name in ('tone.m4a', 'tone_faststart.m4a'):
            with self.subTest(name=name):
                blob = fixture(name)
                result = check(blob, 'mp4', before=b'\x05' * 13, after=b'\x06' * 300)
                self.assertEqual((result.integrity, result.file_type, result.size), ('audio_structure_checked', 'm4a', len(blob)))
                self.assertEqual((result.details['format'], result.details['tracks']), ('m4a', 1))
        # Audio-only MPEG-4 with a generic major brand is still audio.
        generic = fixture('tone.m4a').replace(b'ftypM4A ', b'ftypisom', 1)
        self.assertEqual(check(generic, 'mp4').file_type, 'm4a')
        truncated = check(fixture('tone.m4a')[:-40], 'mp4')
        self.assertEqual((truncated.integrity, truncated.file_type), ('audio_truncated', 'm4a'))

    def test_video_is_unchanged(self):
        result = check(movie(), 'mp4')
        self.assertEqual((result.integrity, result.file_type), ('video_structure_checked', 'mp4'))


def disk(blobs, seed=6):
    rng = random.Random(seed)
    data, at = bytearray(rng.randbytes(2000)), {}
    for name, blob in blobs:
        at[len(data)] = (name, blob); data += blob + rng.randbytes(rng.randrange(400, 2500))
    return bytes(data), at


class ScanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = AppConfig(self.root / 'work'); self.config.initialize()
        self.repo = Repository(self.config.database)

    def blobs(self):
        return [(p.name, p.read_bytes()) for p in sorted(FIXTURES.iterdir()) if p.suffix != '.md']

    def test_scan_finds_each_file_once_and_other_riff_forms_add_nothing(self):
        data, at = disk(self.blobs() + [('clip.avi', wav(form=b'AVI ')), ('pic.webp', wav(form=b'WEBP')), ('video.mp4', movie())])
        path = self.root / 'disk.img'; path.write_bytes(data)
        items = {i.offset: i for i in SignatureScanner().scan(path, CancellationToken())}
        self.assertEqual(set(items), {o for o, (n, _) in at.items() if not n.endswith(('.avi', '.webp'))})
        for offset, item in items.items():
            name, blob = at[offset]
            with self.subTest(name=name):
                if name == 'tone_noxing.mp3':
                    self.assertEqual(item.integrity, 'audio_boundary_unknown'); continue
                expected = 'video_structure_checked' if name.endswith('.mp4') else 'audio_structure_checked'
                self.assertEqual((item.integrity, item.size, item.sha256), (expected, len(blob), hashlib.sha256(blob).hexdigest()))
                self.assertEqual(item.file_type, Path(name).suffix[1:])

    def test_computer_signature_scan_matches(self):
        data, at = disk(self.blobs())
        path = self.root / 'raw.img'; path.write_bytes(data)
        found = {i.offset: (i.file_type, i.integrity) for i in ComputerScanner(self.config.data_dir).scan(
            path, CancellationToken(), kind='image', mode='signature', partition_offset=0) if hasattr(i, 'file_type')}
        self.assertEqual(found, {o: (Path(n).suffix[1:], 'audio_boundary_unknown' if 'noxing' in n else 'audio_structure_checked')
                                 for o, (n, _) in at.items()})

    def test_checked_audio_exports_byte_identical(self):
        data, at = disk(self.blobs())
        path = self.root / 'disk.img'; path.write_bytes(data)
        source = DataSource('image', str(path), path.name)
        task = self.repo.create_task('signature_scan', source)
        items = [replace(i, source_id=source.id) for i in SignatureScanner().scan(path, CancellationToken())]
        self.repo.add_candidates(task, items); self.repo.update_task(task, 'completed')
        ready = [i for i in items if i.integrity == 'audio_structure_checked']
        self.assertEqual(len(ready), len(items) - 1)
        for item in ready:
            self.assertEqual(read_candidate(self.repo, item.id, self.config.data_dir, CancellationToken()), at[item.offset][1])
        export = self.repo.create_task('export')
        # The boundary-unknown MP3 is refused, so the batch reports a failed item.
        with self.assertRaises(ExportError):
            export_batch(self.repo, export, [i.id for i in items], self.root / 'out', self.config.data_dir, CancellationToken())
        records = {r['candidate_id']: r for r in self.repo.exports(export)}
        for item in items:
            record = records[item.id]
            if item.integrity != 'audio_structure_checked':
                self.assertNotEqual(record['status'], 'exported'); continue
            self.assertEqual(record['status'], 'exported')
            self.assertEqual(Path(record['output_path']).read_bytes(), at[item.offset][1])

    def test_resume_with_audio_matches_an_uninterrupted_scan(self):
        data, _ = disk(self.blobs() * 2 + [('clip.avi', wav(form=b'AVI '))], seed=12)
        path = self.root / 'disk.img'; path.write_bytes(data)
        manager = TaskManager(self.repo); self.addCleanup(manager.shutdown)

        def run(operation, resume_task=None):
            source = DataSource('image', str(path), path.name) if resume_task is None else SimpleNamespace(
                id=self.repo.task(resume_task)['source_id'])
            handle = manager.submit('signature_scan', operation, persist_candidates=True, source=source,
                                    batch_size=1, resume_task=resume_task)
            try:
                handle.future.result(60)
            except TaskCancelled:
                pass
            return handle.id

        def scan(limit=None):
            def operation(token):
                items = SignatureScanner(block_size=2048).scan(path, token, checkpoint_every=0)
                return interrupt_after(items, token, limit) if limit else items
            return operation

        reference = summary(self.repo.candidates(run(scan())))
        self.assertEqual(len(reference), 18)
        for limit in (1, 3, 8, 15, 30):
            with self.subTest(limit=limit):
                task = run(scan(limit))
                plan = plan_resume(self.repo, task)
                verify_resume(self.repo, plan, self.config.data_dir)
                run(resume_operation(plan, self.config.data_dir), task)
                self.assertEqual(summary(self.repo.candidates(task)), reference)


class PresentationTests(unittest.TestCase):
    def test_statuses_exportability_and_categories(self):
        statuses = ('audio_structure_checked', 'audio_truncated', 'audio_invalid_structure', 'audio_validation_limit',
                    'audio_unsupported', 'audio_boundary_unknown', 'audio_fragmented', 'audio_encrypted', 'audio_external_reference')
        for status in statuses:
            self.assertNotEqual(content_check(status).label, '未验证的检查结果', status)
        self.assertEqual([s for s in statuses if s in CHECKED], ['audio_structure_checked'])
        self.assertEqual([s for s in statuses if s in INVALID], ['audio_invalid_structure'])
        self.assertTrue(all(checked_signature('audio_structure_checked', kind) for kind in AUDIO_TYPES))
        self.assertFalse(checked_signature('audio_structure_checked', 'flac'))
        self.assertFalse(checked_signature('video_structure_checked', 'm4a'))
        self.assertTrue(all(category(kind) == 'audio' for kind in AUDIO_TYPES))

    def test_check_details(self):
        texts = {}
        for name in ('tone_pcm16.wav', 'tone_vbr.mp3', 'tone.m4a'):
            result = check(fixture(name), KIND[Path(name).suffix])
            texts[name] = audio_check_details({'integrity': result.integrity, 'structure_details': result.details})
        self.assertIn('WAV PCM · 1 声道 · 8000 Hz · 16 位 · 约 1.0 秒', texts['tone_pcm16.wav'])
        self.assertIn('Xing / Info 头记录的字节数', texts['tone_vbr.mp3'])
        self.assertIn('M4A · 1 个 AAC 音轨', texts['tone.m4a'])
        mp3 = check(fixture('tone_cbr.mp3'), 'mp3').details
        for forged in ({**mp3, 'frames': 1}, {**mp3, 'decoded': True}, {**mp3, 'sample_rate': 0}, {**mp3, 'check_version': 2},
                       {**mp3, 'format': 'flac'}, None):
            self.assertEqual(audio_check_details({'integrity': 'audio_structure_checked', 'structure_details': forged}), '')
        self.assertEqual(audio_check_details({'integrity': 'audio_truncated', 'structure_details': mp3}), '')


if __name__ == '__main__':
    unittest.main()
