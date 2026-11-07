"""Office (OOXML and OLE2) signature candidates: structure checks, scanning, export and resume."""
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import random
import struct
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

from app.app_config import AppConfig
from core.candidate_io import read_candidate
from core.computer.service import ComputerScanner
from core.export.service import export_batch
from core.scanner.office.validation import OFFICE_TYPES
from core.scanner.resume import plan_resume, resume_operation, verify_resume
from core.scanner.signature.scanner import SignatureScanner
from core.scanner.validation import CHECKED, SIGNATURES, checked_signature, find_signature, validate
from core.tasks import CancellationToken, TaskCancelled, TaskManager
from storage.database.repository import Repository
from storage.models.classification import category
from storage.models.source import DataSource
from tests import office_fixture as fx
from tests.test_resume import interrupt_after, summary
from tests.test_scanner import png_image
from ui.recovery.presentation import content_check, office_check_details

FIXTURES = Path(__file__).parent / 'fixtures' / 'office'
TOKEN = CancellationToken()


def check(blob, kind, *, before=b'', after=b'', max_size=64 << 20, max_decoded=256 << 20):
    data = before + blob + after
    return validate(io.BytesIO(data), len(before), len(data), kind, TOKEN, max_size, max_decoded)


def patched(blob, at, value):
    data = bytearray(blob); data[at:at + len(value)] = value; return bytes(data)


class OoxmlTests(unittest.TestCase):
    def test_real_libreoffice_files_have_exact_boundaries(self):
        for name, kind in (('libreoffice.docx', 'docx'), ('libreoffice.xlsx', 'xlsx'), ('libreoffice.pptx', 'pptx')):
            with self.subTest(name=name):
                blob = (FIXTURES / name).read_bytes()
                result = check(blob, 'ooxml', before=b'\0' * 333, after=b'\xff' * 999)
                self.assertEqual((result.integrity, result.file_type, result.size), ('office_structure_checked', kind, len(blob)))
                self.assertEqual(result.details['boundary_basis'], 'end_of_central_directory')
        # LibreOffice spreadsheets do not start with [Content_Types].xml.
        first = zipfile.ZipFile(FIXTURES / 'libreoffice.xlsx').namelist()[0]
        self.assertEqual(first, 'xl/_rels/workbook.xml.rels')

    def test_supported_writer_variants(self):
        variants = {'docx': fx.ooxml('docx'), 'xlsx': fx.ooxml('xlsx'), 'pptx': fx.ooxml('pptx'),
                    'libreoffice order': fx.ooxml(order=['_rels/.rels', 'docProps/core.xml', 'word/document.xml', '[Content_Types].xml']),
                    'main part first': fx.ooxml(order=['word/document.xml', '[Content_Types].xml', '_rels/.rels', 'docProps/core.xml']),
                    'data descriptors': fx.ooxml(descriptors=True), 'stored with comment': fx.ooxml(method=zipfile.ZIP_STORED, comment=b'note')}
        for label, blob in variants.items():
            with self.subTest(label=label):
                result = check(blob, 'ooxml', before=b'junk', after=b'tail')
                self.assertEqual((result.integrity, result.size), ('office_structure_checked', len(blob)))
                self.assertIn(result.file_type, OFFICE_TYPES)

    def test_ordinary_zips_inner_entries_and_opendocument_are_not_candidates(self):
        blob = fx.ooxml()
        inner = blob.find(b'PK\x03\x04', 4)
        self.assertGreater(inner, 0)
        self.assertIsNone(check(blob[inner:], 'ooxml', before=blob[:inner]))
        self.assertIsNone(check(fx.plain_zip(), 'ooxml'))
        target = io.BytesIO()
        with zipfile.ZipFile(target, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text'); archive.writestr('content.xml', '<x/>')
        self.assertIsNone(check(target.getvalue(), 'ooxml'))

    def test_damaged_packages_are_reported_but_not_checked(self):
        stored = fx.ooxml(method=zipfile.ZIP_STORED)
        text = stored.find(b'x' * 50)
        self.assertEqual(check(patched(stored, text, b'y'), 'ooxml').integrity, 'office_invalid_structure')   # CRC-32
        self.assertEqual(check(stored[:-30], 'ooxml').integrity, 'office_truncated')
        self.assertEqual(check(stored[:len(stored) // 2], 'ooxml').integrity, 'office_truncated')
        # A non-usual first entry stays silent when the archive cannot be followed.
        self.assertIsNone(check((FIXTURES / 'libreoffice.xlsx').read_bytes()[:3000], 'ooxml'))
        main, kind = fx.MAIN['docx']
        cases = {
            'missing main part': fx.ooxml(rels=fx.root_rels('word/missing.xml')),
            'doctype': fx.ooxml(types=b'<?xml version="1.0"?><!DOCTYPE t [<!ENTITY a "b">]>' + fx.content_types(main, kind)[38:]),
            'malformed xml': fx.ooxml(body=b'<document><body>'),
            'no content types': fx.ooxml(order=['_rels/.rels', 'docProps/core.xml', main], types=b''),
        }
        for label, blob in cases.items():
            with self.subTest(label=label):
                self.assertEqual(check(blob, 'ooxml').integrity, 'office_invalid_structure')
        self.assertEqual(check(fx.ooxml(types=fx.content_types(main, 'application/xml')), 'ooxml').integrity, 'office_unsupported')

    def test_encryption_macros_and_activex_are_not_exportable(self):
        blob = fx.ooxml()
        central = blob.find(b'PK\x01\x02')
        flags = struct.unpack_from('<H', blob, central + 8)[0]
        self.assertEqual(check(patched(blob, central + 8, struct.pack('<H', flags | 1)), 'ooxml').integrity, 'office_encrypted')
        for label, blob in (('docm', fx.ooxml('docm')), ('vba part', fx.ooxml(parts={'word/vbaProject.bin': b'\0' * 64})),
                            ('activex', fx.ooxml(parts={'word/activeX/activeX1.xml': b'<x/>'}))):
            with self.subTest(label=label):
                result = check(blob, 'ooxml')
                self.assertEqual(result.integrity, 'office_active_content')
                self.assertFalse(checked_signature(result.integrity, result.file_type))

    def test_decompression_and_size_budgets(self):
        bomb = fx.ooxml(parts={'word/media/blank.bin': b'\0' * 2_000_000})
        self.assertLess(len(bomb), 10_000)
        self.assertEqual(check(bomb, 'ooxml', max_decoded=1_000_000).integrity, 'office_validation_limit')
        self.assertEqual(check(bomb, 'ooxml').integrity, 'office_structure_checked')
        blob = fx.ooxml()
        self.assertEqual(check(blob, 'ooxml', after=b'\0' * 100, max_size=len(blob) - 1).integrity, 'office_validation_limit')


class OleTests(unittest.TestCase):
    def test_real_libreoffice_compound_files(self):
        for name, kind in (('libreoffice.doc', 'doc'), ('libreoffice.xls', 'xls')):
            with self.subTest(name=name):
                blob = (FIXTURES / name).read_bytes()
                result = check(blob, 'ole', before=b'\x11' * 100, after=b'\x22' * 700)
                self.assertEqual((result.integrity, result.file_type, result.size), ('office_structure_checked', kind, len(blob)))
                self.assertEqual(result.details['boundary_basis'], 'highest_allocated_sector')

    def test_generated_versions_and_types(self):
        for kind, major in (('doc', 3), ('xls', 3), ('ppt', 3), ('doc', 4)):
            with self.subTest(kind=kind, major=major):
                blob = fx.ole(kind, major=major)
                result = check(blob, 'ole', before=b'ab', after=b'cd')
                self.assertEqual((result.integrity, result.file_type, result.size), ('office_structure_checked', kind, len(blob)))
                self.assertEqual(result.details['sector_size'], 512 if major == 3 else 4096)

    def test_unrelated_encrypted_and_macro_compound_files(self):
        self.assertIsNone(check(fx.ole('msg'), 'ole'))
        encrypted = fx.ole(streams={'EncryptedPackage': b'e' * 5000, 'EncryptionInfo': b'i' * 200})
        self.assertEqual(check(encrypted, 'ole').integrity, 'office_encrypted')
        for storage, kind in (('Macros', 'doc'), ('_VBA_PROJECT_CUR', 'xls')):
            with self.subTest(storage=storage):
                self.assertEqual(check(fx.ole(kind, storages=(storage,)), 'ole').integrity, 'office_active_content')

    def test_damaged_sector_tables(self):
        blob = fx.ole(streams={'WordDocument': b'W' * 5000, 'Data': b'D' * 5000, 'Small': b's' * 100})
        first_dir = struct.unpack_from('<I', blob, 48)[0]
        directory = (first_dir + 1) * 512
        fat = 512
        word_start = struct.unpack_from('<I', blob, directory + 128 + 116)[0]
        cases = {
            'loop': patched(blob, fat + 4 * first_dir, struct.pack('<I', first_dir)),
            'shared sectors': patched(blob, directory + 256 + 116, struct.pack('<I', word_start)),
            'wrong stream size': patched(blob, directory + 128 + 120, struct.pack('<I', 9000)),
            'byte order': patched(blob, 28, b'\0\0'),
            'mini chain past mini stream': patched(blob, directory + 384 + 116, struct.pack('<I', 500)),
        }
        for label, damaged in cases.items():
            with self.subTest(label=label):
                self.assertEqual(check(damaged, 'ole').integrity, 'office_invalid_structure')
        self.assertEqual(check(blob[:-600], 'ole').integrity, 'office_truncated')
        self.assertEqual(check(blob[:300], 'ole').integrity, 'office_truncated')


def disk(blobs, seed=3):
    rng = random.Random(seed)
    data, at = bytearray(rng.randbytes(3000)), {}
    for name, blob in blobs:
        at[len(data)] = (name, blob); data += blob + rng.randbytes(rng.randrange(500, 3000))
    return bytes(data), at


class ScanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = AppConfig(self.root / 'work'); self.config.initialize()
        self.repo = Repository(self.config.database)

    def office_blobs(self):
        real = [(p.name, p.read_bytes()) for p in sorted(FIXTURES.iterdir()) if p.suffix != '.md']
        return real + [('made.pptx', fx.ooxml('pptx')), ('made.ppt', fx.ole('ppt')), ('made-v4.doc', fx.ole('doc', major=4))]

    def test_scan_finds_each_document_once_and_ordinary_archives_add_nothing(self):
        many = fx.plain_zip([f'folder/item{i}.txt' for i in range(3000)])   # far more entries than the candidate limit allows
        picture = png_image()
        with_picture = fx.ooxml(parts={'word/media/image1.png': picture}, method=zipfile.ZIP_STORED)
        noise = [('archive.zip', many), ('outlook.msg', fx.ole('msg')), ('picture-in-zip.zip', _stored_zip(picture))]
        data, at = disk(self.office_blobs() + [('picture.docx', with_picture)] + noise)
        path = self.root / 'disk.img'; path.write_bytes(data)
        items = list(SignatureScanner().scan(path, CancellationToken()))
        office = {i.offset: i for i in items if i.file_type in OFFICE_TYPES}
        expected = {o for o, (n, _) in at.items() if n.endswith(('.docx', '.xlsx', '.pptx', '.doc', '.xls', '.ppt'))}
        self.assertEqual(set(office), expected)
        for offset, item in office.items():
            self.assertEqual((item.integrity, item.size), ('office_structure_checked', len(at[offset][1])))
            self.assertEqual(item.sha256, hashlib.sha256(at[offset][1]).hexdigest())
        # The PNG inside the checked docx is covered by it; the one in an ordinary zip is reported.
        pictures = [i.offset for i in items if i.file_type == 'png']
        zipped = next(o for o, (n, _) in at.items() if n == 'picture-in-zip.zip')
        self.assertEqual(len(pictures), 1)
        self.assertTrue(zipped < pictures[0] < zipped + len(at[zipped][1]))
        self.assertEqual(len(items), len(expected) + 1)

    def test_computer_signature_scan_matches(self):
        data, at = disk(self.office_blobs())
        path = self.root / 'raw.img'; path.write_bytes(data)
        items = [i for i in ComputerScanner(self.config.data_dir).scan(path, CancellationToken(), kind='image', mode='signature',
                                                                        partition_offset=0) if hasattr(i, 'file_type')]
        found = {i.offset: (i.file_type, i.integrity, i.size) for i in items}
        self.assertEqual(found, {o: (Path(n).suffix[1:], 'office_structure_checked', len(b)) for o, (n, b) in at.items()})

    def test_checked_documents_export_byte_identical(self):
        data, at = disk(self.office_blobs())
        path = self.root / 'disk.img'; path.write_bytes(data)
        source = DataSource('image', str(path), path.name)
        task = self.repo.create_task('signature_scan', source)
        items = [replace(i, source_id=source.id) for i in SignatureScanner().scan(path, CancellationToken())]
        self.repo.add_candidates(task, items); self.repo.update_task(task, 'completed')
        for item in items:   # The guarded read path used by previews re-checks identity and digest.
            self.assertEqual(read_candidate(self.repo, item.id, self.config.data_dir, CancellationToken()), at[item.offset][1])
        export = self.repo.create_task('export')
        export_batch(self.repo, export, [i.id for i in items], self.root / 'out', self.config.data_dir, CancellationToken())
        records = self.repo.exports(export)
        self.assertEqual([r['status'] for r in records], ['exported'] * len(items))
        for record in records:
            offset = next(i.offset for i in items if i.id == record['candidate_id'])
            self.assertEqual(Path(record['output_path']).read_bytes(), at[offset][1])
            self.assertEqual(Path(record['output_path']).suffix, '.' + next(i.file_type for i in items if i.id == record['candidate_id']))

    def test_resume_with_office_documents_matches_an_uninterrupted_scan(self):
        data, _ = disk(self.office_blobs() * 2 + [('archive.zip', fx.plain_zip([f'a{i}' for i in range(200)]))], seed=9)
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

        scan = lambda limit=None: (lambda token: interrupt_after(SignatureScanner(block_size=4096).scan(path, token, checkpoint_every=0), token, limit)
                                   if limit else SignatureScanner(block_size=4096).scan(path, token, checkpoint_every=0))
        reference = summary(self.repo.candidates(run(scan())))
        self.assertEqual(len(reference), 16)
        for limit in (1, 2, 5, 9, 17):
            with self.subTest(limit=limit):
                task = run(scan(limit))
                plan = plan_resume(self.repo, task)
                verify_resume(self.repo, plan, self.config.data_dir)
                run(resume_operation(plan, self.config.data_dir), task)
                self.assertEqual(self.repo.task(task)['status'], 'completed')
                self.assertEqual(summary(self.repo.candidates(task)), reference)


class SignatureCacheTests(unittest.TestCase):
    def test_cached_search_matches_uncached_search(self):
        rng = random.Random(4)
        pieces = [sig for sig, _ in SIGNATURES] + [b'PK\x03', b'\x89PNG', b'%PDF', b'fty']

        def walk(data, start, steps, cache):
            found, cursor = [], start
            for step in steps:
                match = find_signature(data, cursor, cache)
                if match is None:
                    break
                found.append(match); cursor = match[0] + step
            return found

        for trial in range(60):
            data = bytearray(rng.randbytes(rng.randrange(0, 4000)))
            for _ in range(rng.randrange(0, 80)):
                at = rng.randrange(0, len(data) + 1); data[at:at] = rng.choice(pieces)
            data = bytes(data)
            steps = [rng.choice((1, 1, 2, 4, 9, 30)) for _ in range(200)]
            for start in (0, 1, 3, 4, 5, len(data) // 2):
                with self.subTest(trial=trial, start=start):
                    plain = walk(data, start, steps, None)
                    self.assertEqual(walk(data, start, steps, {}), plain)


def _stored_zip(member):
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_STORED) as archive:
        archive.writestr('photo.png', member)
    return target.getvalue()


class PresentationTests(unittest.TestCase):
    def test_every_office_status_is_explained_and_only_checked_is_exportable(self):
        statuses = ('office_structure_checked', 'office_truncated', 'office_invalid_structure', 'office_validation_limit',
                    'office_unsupported', 'office_encrypted', 'office_active_content')
        for status in statuses:
            self.assertNotEqual(content_check(status).label, '未验证的检查结果', status)
        self.assertEqual([s for s in statuses if s in CHECKED], ['office_structure_checked'])
        self.assertFalse(checked_signature('office_structure_checked', 'ooxml'))
        self.assertTrue(all(checked_signature('office_structure_checked', kind) for kind in OFFICE_TYPES))
        self.assertTrue(all(category(kind) == 'document' for kind in OFFICE_TYPES | {'ooxml', 'ole'}))

    def test_check_details(self):
        docx = check((FIXTURES / 'libreoffice.docx').read_bytes(), 'ooxml')
        doc = check((FIXTURES / 'libreoffice.doc').read_bytes(), 'ole')
        text = office_check_details({'integrity': docx.integrity, 'structure_details': docx.details})
        self.assertIn('新版 Office 文档', text); self.assertIn('word/document.xml', text)
        self.assertIn('扇区 512 字节', office_check_details({'integrity': doc.integrity, 'structure_details': doc.details}))
        for forged in ({'check_version': 2}, {**docx.details, 'entries': 0}, {**docx.details, 'rendered': True}, None):
            self.assertEqual(office_check_details({'integrity': 'office_structure_checked', 'structure_details': forged}), '')
        self.assertEqual(office_check_details({'integrity': 'office_truncated', 'structure_details': docx.details}), '')


if __name__ == '__main__':
    unittest.main()
