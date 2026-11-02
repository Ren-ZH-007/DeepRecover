import base64
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from core.scanner.signature.scanner import SignatureScanner, ScanError
from core.tasks import CancellationToken, TaskCancelled, TaskManager
from storage.database.repository import Repository
from storage.models.source import DataSource


# A real 2x2 RGB JPEG encoded by Qt, kept inline so core tests need no Qt.
JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAQEAZABkAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDnKKKK948Q/9k=')

PROGRESSIVE_JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAQEAZABkAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wgARCAACAAIDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUAQEAAAAAAAAAAAAAAAAAAAAE/9oADAMBAAIQAxAAAAGaHh//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAEFAn//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/AX//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/AX//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAY/An//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/IX//2gAMAwEAAgADAAAAEPf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/EH//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/EH//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/EH//2Q==')


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


def png_image(width=1, height=1, raw=b'\x00\x12\x34\x56', interlace=0, before=b'', after=b''):
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, interlace))
            + before + chunk(b'IDAT', zlib.compress(raw)) + after + chunk(b'IEND', b''))


class RecordingToken(CancellationToken):
    def __init__(self):
        super().__init__()
        self.history = []

    def report_progress(self, processed, total, candidates):
        super().report_progress(processed, total, candidates)
        self.history.append(self.progress)


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'image.bin'

    def scan(self, data, token=None, **options):
        self.source.write_bytes(data)
        return list(SignatureScanner(**options).scan(self.source, token or CancellationToken()))

    def test_mixed_real_images_exact_offsets_lengths_and_read_only(self):
        png = png_image()
        data = b'padding' + JPEG + b'gap' + png + b'trailer'
        self.source.write_bytes(data)
        original_open = Path.open
        modes = []
        def tracked_open(path, mode='r', *args, **kwargs):
            if path == self.source:
                modes.append(mode)
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', tracked_open):
            results = list(SignatureScanner(block_size=31).scan(self.source, CancellationToken()))
        self.assertEqual(modes, ['rb', 'rb'])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), hashlib.sha256(data).digest())
        self.assertEqual([(c.file_type,c.offset,c.size,c.integrity) for c in results],
                         [('jpg',7,len(JPEG),'structure_checked'), ('png',7+len(JPEG)+3,len(png),'structure_checked')])
        self.assertTrue(all(c.status == 'candidate' and not c.local_path and c.sha256 is None for c in results))
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_signatures_cross_every_block_boundary_without_duplicates(self):
        for image in (png_image(), JPEG):
            for offset in range(8,16):
                with self.subTest(offset=offset, kind=image[:3]):
                    results = self.scan(b'x'*offset + image, block_size=8)
                    self.assertEqual(len(results),1)
                    self.assertEqual((results[0].offset,results[0].size,results[0].integrity),(offset,len(image),'structure_checked'))

    def test_truncated_png_and_jpeg_are_partial(self):
        for image in (png_image()[:-5], JPEG[:-2], b'\xff\xd8\xff'):
            with self.subTest(image=image[:3]):
                result = self.scan(b'x'*9+image)[0]
                self.assertEqual((result.offset,result.size,result.integrity,result.status),(9,len(image),'truncated','partial'))

    def test_bad_crc_does_not_hide_a_later_valid_signature(self):
        bad = bytearray(png_image()); bad[29] ^= 1
        valid = png_image()
        results = self.scan(bytes(bad)+b'gap'+valid, block_size=19)
        self.assertEqual([c.integrity for c in results],['invalid_structure','structure_checked'])
        self.assertEqual(results[-1].offset,len(bad)+3)

    def test_png_rejects_bad_filter_length_zlib_and_trailing_zlib_data(self):
        for image in (png_image(raw=b'\x05abc'),png_image(raw=b'\x00ab'),png_image(raw=b'\x00abcx')):
            self.assertEqual(self.scan(image)[0].integrity,'invalid_structure')
        header = png_image()[:33]
        for compressed in (b'not zlib',zlib.compress(b'\x00abc')+b'trailing'):
            image = header+chunk(b'IDAT',compressed)+chunk(b'IEND',b'')
            self.assertEqual(self.scan(image)[0].integrity,'invalid_structure')

    def test_png_split_idat_and_adam7_layout(self):
        header = png_image()[:33]
        compressed = zlib.compress(b'\x00abc')
        image = header + b''.join(chunk(b'IDAT',bytes([x])) for x in compressed) + chunk(b'IEND',b'')
        self.assertEqual(self.scan(image)[0].integrity,'structure_checked')
        self.assertEqual(self.scan(png_image(interlace=1))[0].integrity,'structure_checked')
        # 3x3 RGB Adam7 pass row counts: 1, 0, 0, 1, 1, 2, 1.
        layouts = [(1,1),(1,1),(2,1),(1,2),(3,1)]
        raw = b''.join((b'\x00'+b'\x22'*(width*3))*height for width,height in layouts)
        self.assertEqual(self.scan(png_image(width=3,height=3,raw=raw,interlace=1))[0].integrity,'structure_checked')

    def test_png_checks_critical_chunk_order_and_header_constraints(self):
        signature = b'\x89PNG\r\n\x1a\n'
        valid = png_image()
        cases = [signature+chunk(b'IEND',b''),png_image(before=chunk(b'ABCD',b'')),
                 png_image(before=valid[8:33]),png_image(width=0),
                 valid[:-12]+chunk(b'IEND',b'x'),
                 valid[:-12]+chunk(b'tEXt',b'key\x00value')+chunk(b'IDAT',b'')+valid[-12:]]
        for image in cases:
            with self.subTest(image=image[:40]):
                self.assertEqual(self.scan(image)[0].integrity,'invalid_structure')

    def test_png_indexed_color_requires_valid_palette(self):
        header=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,1,3,0,0,0))
        tail=chunk(b'IDAT',zlib.compress(b'\x00\x00'))+chunk(b'IEND',b'')
        self.assertEqual(self.scan(header+tail)[0].integrity,'invalid_structure')
        self.assertEqual(self.scan(header+chunk(b'PLTE',b'\x00\x00\x00')+tail)[0].integrity,'structure_checked')
        self.assertEqual(self.scan(header+chunk(b'PLTE',b'\x00'*9)+tail)[0].integrity,'invalid_structure')

    def test_embedded_signatures_inside_valid_containers_are_not_separate_results(self):
        image = png_image(before=chunk(b'tEXt',b'note\x00'+JPEG+png_image()))
        self.assertEqual(len(self.scan(image,block_size=16)),1)
        app = b'\xff\xef'+struct.pack('>H',len(image)+4)+b'\xff\xd9'+image
        jpeg = JPEG[:2]+app+JPEG[2:]
        results = self.scan(jpeg,block_size=16)
        self.assertEqual([(c.size,c.integrity) for c in results],[(len(jpeg),'structure_checked')])

    def test_jpeg_rejects_missing_tables_invalid_lengths_and_frame(self):
        for image in (b'\xff\xd8\xff\xd9',b'\xff\xd8\xff\xe0\x00\x01',
                      JPEG.replace(b'\xff\xc0\x00\x11\x08\x00\x02',b'\xff\xc0\x00\x11\x08\x00\x00',1),
                      JPEG.replace(b'\xff\xc4',b'\xff\xee')):
            self.assertEqual(self.scan(image)[0].integrity,'invalid_structure')

    def test_jpeg_stuffed_ff_and_restart_markers_do_not_end_image(self):
        image = JPEG[:2]+b'\xff\xdd\x00\x04\x00\x01'+JPEG[2:-2]+b'\xff\x00\x11\xff\xd0\x22'+JPEG[-2:]
        self.assertEqual(self.scan(image)[0].integrity,'structure_checked')
        self.assertEqual(self.scan(image)[0].size,len(image))

    def test_real_progressive_jpeg_with_multiple_scans(self):
        result=self.scan(b'gap'+PROGRESSIVE_JPEG,block_size=17)
        self.assertEqual([(c.offset,c.size,c.integrity) for c in result],[(3,len(PROGRESSIVE_JPEG),'structure_checked')])

    def test_jpeg_empty_scan_and_restart_without_interval_are_invalid(self):
        start=JPEG.index(b'\xff\xda')
        end=start+2+struct.unpack('>H',JPEG[start+2:start+4])[0]
        for image in (JPEG[:end]+b'\xff\xd9',JPEG[:-2]+b'\xff\xd0'+JPEG[-2:]):
            self.assertEqual(self.scan(image)[0].integrity,'invalid_structure')

    def test_resource_limits_do_not_claim_validated_images(self):
        for image,options in ((png_image(),{'max_file_size':40}),
                              (png_image(width=100000,height=100000),{'max_decoded':1024}),
                              (JPEG[:-2]+b'x'*10000+JPEG[-2:],{'max_file_size':1024})):
            with self.subTest(options=options):
                result=self.scan(image,**options)[0]
                self.assertEqual((result.integrity,result.status),('validation_limit','partial'))
                self.assertLessEqual(result.size,options.get('max_file_size',64*1024*1024))

    def test_progress_is_monotonic_and_finishes_for_empty_and_no_match_files(self):
        for data in (b'',b'x'*100,b'xxxxxx'+png_image()+b'x'*100+JPEG):
            token=RecordingToken()
            results=self.scan(data,token=token,block_size=8)
            self.assertEqual(token.progress.processed,len(data))
            self.assertEqual(token.progress.candidates,len(results))
            self.assertEqual([p.processed for p in token.history],sorted(p.processed for p in token.history))

    def test_cancellation_before_scan_and_between_candidates(self):
        token=CancellationToken(); token.cancel()
        with self.assertRaises(TaskCancelled): self.scan(JPEG,token=token)
        self.source.write_bytes(png_image()+b'x'*20+JPEG)
        token=CancellationToken()
        iterator=SignatureScanner(block_size=8).scan(self.source,token)
        next(iterator); token.cancel()
        with self.assertRaises(TaskCancelled): next(iterator)

    def test_cancellation_inside_large_container_validation(self):
        class Token(CancellationToken):
            def __init__(self): super().__init__(); self.checks=0
            def checkpoint(self):
                self.checks+=1
                if self.checks==30: self.cancel()
                super().checkpoint()
        token=Token()
        with self.assertRaises(TaskCancelled): self.scan(JPEG[:-2]+b'x'*(4*1024*1024)+JPEG[-2:],token=token)
        self.assertEqual(token.checks,30)

    def test_changed_source_causes_failure_not_complete_progress(self):
        for trailing in (b'',b'x'*50):
            self.source.write_bytes(png_image()+trailing)
            token=CancellationToken()
            iterator=SignatureScanner(block_size=16).scan(self.source,token)
            next(iterator)
            self.source.write_bytes(self.source.read_bytes()+b'changed')
            with self.assertRaises(ScanError): list(iterator)
            self.assertLess(token.progress.processed,token.progress.total)

    def test_candidate_limit_marks_task_failed_and_retains_partial_history(self):
        self.source.write_bytes(png_image()+JPEG)
        repo=Repository(self.root/'index.db')
        manager=TaskManager(repo); self.addCleanup(manager.shutdown)
        with self.assertLogs('deeprecover',level='WARNING'):
            task=manager.submit('signature_scan',lambda token: SignatureScanner(max_candidates=1).scan(self.source,token),
                source=DataSource('image',str(self.source)),persist_candidates=True,batch_size=1)
            with self.assertRaises(ScanError): task.future.result(5)
        self.assertEqual(repo.task(task.id)['status'],'failed')
        self.assertEqual(repo.candidate_count(task.id),1)
        stored=Repository(repo.path).candidate_page(task.id)[0]
        self.assertEqual(stored['offset'],0)
        self.assertEqual(repo.source(stored['source_id'])['locator'],str(self.source))

    def test_invalid_source_and_options_fail(self):
        for source in (self.root,self.root/'missing'):
            with self.assertRaises(ScanError): list(SignatureScanner().scan(source,CancellationToken()))
        for options in ({'block_size':7},{'max_file_size':0},{'max_decoded':0},{'max_candidates':0}):
            with self.assertRaises(ValueError): SignatureScanner(**options)

    def test_progress_rejects_inconsistent_snapshots(self):
        token=CancellationToken(); token.report_progress(5,10,1)
        for values in ((4,10,1),(6,11,1),(6,10,0),(11,10,1)):
            with self.assertRaises(ValueError): token.report_progress(*values)
