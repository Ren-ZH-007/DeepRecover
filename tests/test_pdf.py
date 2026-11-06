"""PDF structure/range adversaries and verified local rendering integration."""
from dataclasses import replace
import hashlib
from importlib.resources import files
from io import BytesIO
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from app.app_config import AppConfig
from core.candidate_io import CandidateAccessError, candidate_input, snapshot
from core.computer.io import DiskReader
from core.computer.partitions import Partition
from core.computer.service import ComputerScanner
from core.export.service import export_batch
from core.preview.pdf import inspect_pdf, prepare_pdf, render_pdf, MAX_RENDER_RSS
from core.scanner.pdf.validation import validate, MAX_BYTES
from core.scanner.signature.scanner import SignatureScanner
from core.tasks import CancellationToken, TaskCancelled
from storage.database.repository import Repository
from storage.models.source import DataSource
from storage.models.candidate import Candidate
from tests.pdf_fixture import make_pdf, objects, stream
from tests.test_scanner import png_image
from ui.recovery.presentation import content_check, deletion_evidence, pdf_check_details

try:
    import PySide6.QtGui, PySide6.QtPdf  # noqa: F401  # renderer and pixel decoding
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False
requires_qt = unittest.skipUnless(QT_AVAILABLE, 'PySide6 not installed')


class PdfStructureTests(unittest.TestCase):
    def check(self,data,*,start=0,end=None,limit=MAX_BYTES,token=None):
        return validate(BytesIO(data),start,len(data) if end is None else end,token or CancellationToken(),limit)

    def test_real_two_page_pdf_exact_eof_boundary_without_unrelated_trailing_bytes(self):
        data=make_pdf(); result=self.check(b'prefix'+data+b'unrelated',start=6)
        self.assertEqual((result.size,result.integrity),(len(data),'pdf_structure_checked'))
        self.assertEqual((result.details['pages'],result.details['objects'],result.details['rendered']),(2,7,False))

    def test_stream_length_can_be_indirect_and_out_of_object_order(self):
        values=objects();payload=b'BT /F1 12 Tf 20 200 Td (Indirect length) Tj ET'
        values[4]=stream(payload,indirect_length=8); values[8]=str(len(payload)).encode()
        self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_structure_checked')

    def test_eof_and_startxref_inside_stream_are_not_used_as_boundaries(self):
        values=objects(); values[4]=stream(b'BT (\nstartxref\n10\n%%EOF\n) Tj ET')
        data=make_pdf(values);result=self.check(data)
        self.assertEqual((result.integrity,result.size),('pdf_structure_checked',len(data)))

    def test_xref_stream_like_footer_inside_literal_stream_does_not_hide_real_eof(self):
        values=objects()
        payload=b'BT (9 0 obj\n<< /Type /XRef /Length 0 >>\nstream\n\nendstream\nendobj\nstartxref\n0000000000\n%%EOF\n) Tj ET'
        values[4]=stream(payload);data=make_pdf(values)
        false_offset=data.index(b'9 0 obj')
        data=data.replace(b'startxref\n0000000000\n%%EOF',b'startxref\n'+f'{false_offset:010d}'.encode()+b'\n%%EOF',1)
        result=self.check(data)
        self.assertEqual((result.integrity,result.size),('pdf_structure_checked',len(data)))

    def test_literal_keywords_do_not_execute_or_trigger_false_activity_labels(self):
        values=objects();values[8]=b'<< /Title (Text: /JavaScript /URI /Launch) /Other <4a53> >>'
        self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_structure_checked')

    def test_scripts_forms_and_attachments_are_rejected_including_escaped_names(self):
        for addition in (b'/OpenAction 8 0 R',b'/AcroForm << >>',b'/Names << /EmbeddedFiles << >> >>',b'/#4aS (do not run)',b'/Jav#61Script (do not run)'):
            values=objects();values[1]=values[1][:-2]+addition+b' >>';values[8]=b'null'
            with self.subTest(addition=addition):
                self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_active_content')

    def test_external_file_network_and_stream_references_are_distinct(self):
        for body in (b'<< /Type /Filespec /F (outside.pdf) >>',b'<< /S /URI /URI (https://example.invalid) >>',stream(b'abc',extra=b' /F (outside.bin)')):
            values=objects();values[8]=body
            self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_external_reference')
        values=objects();values[8]=b'<< /A << /S /URI /URI (https://example.invalid) >> >>'
        self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_external_reference')

    def test_encrypted_and_object_stream_documents_are_not_admitted(self):
        self.assertEqual(self.check(make_pdf(trailer=b'/Encrypt 8 0 R')).integrity,'pdf_encrypted')
        values=objects();values[8]=stream(b'not admitted',extra=b' /Type /ObjStm')
        self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_unsupported')
        self.assertEqual(self.check(make_pdf(trailer=b'/XRefStm 20')).integrity,'pdf_unsupported')

    def test_incremental_revision_is_never_exported_as_older_base_pdf(self):
        base=make_pdf()
        for tail in (b'8 0 obj\n<< >>\nendobj\n',b'\n% update\nxref\n',b'\n8 0 obj\ntruncated'):
            self.assertEqual(self.check(base+tail).integrity,'pdf_incremental')
        self.assertEqual(self.check(make_pdf(trailer=b'/Prev 10')).integrity,'pdf_incremental')

    def test_xref_offsets_must_bind_exact_object_ids_and_ranges(self):
        data=make_pdf();at=data.index(b'xref\n')
        entry=data[at:].splitlines()[3]
        offset=int(entry[:10])
        bad=data[:at]+data[at:].replace(entry,f'{offset+1:010d} 00000 n '.encode(),1)
        self.assertEqual(self.check(bad).integrity,'pdf_invalid_structure')
        self.assertEqual(self.check(data.replace(b'1 0 obj',b'9 0 obj',1)).integrity,'pdf_invalid_structure')
        self.assertEqual(self.check(data.replace(b'/Root 1 0 R',b'/Root 9 0 R')).integrity,'pdf_invalid_structure')

    def test_unindexed_objects_and_overlapping_streams_are_rejected(self):
        data=make_pdf();at=data.index(b'1 0 obj')
        # An unindexed object cannot hide active content between registered ranges.
        bad=data[:at]+b'9 0 obj\nnull\nendobj\n'+data[at:]
        self.assertEqual(self.check(bad).integrity,'pdf_invalid_structure')
        values=objects();values[4]=b'<< /Length 999999 >>\nstream\nabc\nendstream'
        self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_invalid_structure')

    def test_page_tree_counts_cycles_parents_and_missing_references_fail(self):
        changes=[(2,b'/Count 2',b'/Count 3'),(2,b'[3 0 R 6 0 R]',b'[2 0 R 6 0 R]'),(3,b'/Parent 2 0 R',b'/Parent 1 0 R'),(3,b'/Contents 4 0 R',b'/Contents 9 0 R')]
        for number,old,new in changes:
            values=objects();values[number]=values[number].replace(old,new)
            with self.subTest(number=number,new=new):self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_invalid_structure')

    def test_truncated_and_partition_bounded_inputs_never_read_outside_source(self):
        data=make_pdf()
        for end in (8,len(data)-8,len(data)-2):
            self.assertNotEqual(self.check(data,end=end).integrity,'pdf_structure_checked')
        class Bounded(BytesIO):
            def read(self,size=-1):
                assert self.tell()+size <= len(data)
                return super().read(size)
        result=validate(Bounded(data+b'other partition'),0,len(data),CancellationToken())
        self.assertEqual(result.integrity,'pdf_structure_checked')

    def test_eof_without_final_newline_is_valid_and_xref_stream_is_explicit(self):
        data=make_pdf().rstrip(b'\n')
        self.assertEqual(self.check(data).integrity,'pdf_structure_checked')
        data=b'%PDF-1.5\n';offset=len(data)
        data+=b'8 0 obj\n<< /Type /XRef /Length 0 >>\nstream\n\nendstream\nendobj\nstartxref\n'+str(offset).encode()+b'\n%%EOF\n'
        self.assertEqual(self.check(data).integrity,'pdf_unsupported')

    def test_file_object_syntax_page_and_decoded_stream_limits_are_explicit(self):
        data=make_pdf()
        self.assertEqual(self.check(data,limit=len(data)-10).integrity,'pdf_validation_limit')
        with patch('core.scanner.pdf.validation.MAX_OBJECTS',4):self.assertEqual(self.check(data).integrity,'pdf_validation_limit')
        with patch('core.scanner.pdf.validation.MAX_TOKENS',8):self.assertEqual(self.check(data).integrity,'pdf_validation_limit')
        with patch('core.scanner.pdf.validation.MAX_PAGES',1):self.assertEqual(self.check(data).integrity,'pdf_validation_limit')
        values=objects();values[4]=stream(b'x'*4096,compressed=True)
        with patch('core.scanner.pdf.validation.MAX_DECODED',1024):self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_validation_limit')

    def test_large_strings_and_deep_nesting_hit_limits_without_recursion_failure(self):
        for payload in (b'('+b'x'*65537+b')',b'['*34+b'null'+b']'*34,b'('*34+b'text'+b')'*34):
            values=objects();values[8]=payload
            with self.subTest(prefix=payload[:4]):
                self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_validation_limit')

    def test_cancellation_propagates_and_bad_header_has_small_read_budget(self):
        token=CancellationToken();token.cancel()
        with self.assertRaises(TaskCancelled):self.check(make_pdf(),token=token)
        class Watched(BytesIO):
            def read(self,size=-1):
                if self.tell()+size>16:raise AssertionError('invalid header read too much')
                return super().read(size)
        self.assertEqual(validate(Watched(b'%PDF-bad'+b'x'*1000),0,1008,CancellationToken()).integrity,'pdf_unsupported')

    def test_nul_escaped_name_and_malformed_flate_do_not_pass(self):
        values=objects();values[8]=b'<< /JS#00 (text) >>'
        self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_invalid_structure')
        values=objects();values[4]=stream(b'not zlib',extra=b' /Filter /FlateDecode')
        self.assertEqual(self.check(make_pdf(values)).integrity,'pdf_invalid_structure')

    def test_deterministic_metadata_mutations_do_not_raise_unhandled_errors(self):
        import random
        data=make_pdf();rng=random.Random(9)
        for _ in range(400):
            bad=bytearray(data);at=rng.randrange(len(data));bad[at]^=rng.randrange(1,256)
            result=self.check(bytes(bad));self.assertTrue(result.integrity.startswith('pdf_'));self.assertLessEqual(result.size,len(data))


class PdfIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.path=self.root/'source.img'
        self.config=AppConfig(self.root/'work');self.config.initialize();self.repo=Repository(self.config.database)
        self.pdf=make_pdf()

    def scan(self,data,**options):
        self.path.write_bytes(data)
        return list(SignatureScanner(**options).scan(self.path,CancellationToken()))

    def persist(self,items,computer=False):
        source=DataSource('computer' if computer else 'image',str(self.path))
        task=self.repo.create_task('computer_scan' if computer else 'signature_scan',source)
        for item in items:self.repo.add_candidate(task,replace(item,source_id=source.id))
        self.repo.update_task(task,'completed');return task

    def test_cross_every_header_block_boundary_no_duplicates(self):
        for offset in range(8,24):
            with self.subTest(offset=offset):
                item,=self.scan(b'x'*offset+self.pdf+b'gap',block_size=8)
                self.assertEqual((item.offset,item.size,item.integrity),(offset,len(self.pdf),'pdf_structure_checked'))
                self.assertEqual(item.sha256,hashlib.sha256(self.pdf).hexdigest())

    def test_adjacent_pdfs_and_comment_gap_are_two_candidates(self):
        gap=b'\n% harmless gap\n'
        items=self.scan(self.pdf+gap+self.pdf,block_size=31)
        self.assertEqual([(c.offset,c.size) for c in items],[(0,len(self.pdf)),(len(self.pdf)+len(gap),len(self.pdf))])

    def test_valid_pdf_suppresses_nested_signature_but_bad_one_does_not_hide_following(self):
        values=objects();values[8]=stream(png_image())
        valid=make_pdf(values);self.assertEqual(len(self.scan(valid)),1)
        values[1]=values[1][:-2]+b' /AcroForm << >> >>'
        items=self.scan(make_pdf(values)+b'gap'+self.pdf)
        self.assertEqual([c.file_type for c in items],['pdf','png','pdf'])
        self.assertEqual(items[-1].integrity,'pdf_structure_checked')

    def test_readonly_scan_document_filter_export_and_source_missing_history(self):
        original=b'prefix'+self.pdf+b'gap';self.path.write_bytes(original)
        original_open=Path.open;modes=[]
        def opened(path,mode='r',*a,**kw):
            if path.resolve()==self.path:modes.append(mode)
            return original_open(path,mode,*a,**kw)
        with patch.object(Path,'open',opened):items=list(SignatureScanner().scan(self.path,CancellationToken()))
        self.assertEqual(modes,['rb','rb']);task=self.persist(items);item=items[0]
        self.assertEqual(self.repo.candidate_count(task,category='document'),1)
        candidate=self.repo.candidate_page(task,category='document',limit=1)[0]
        self.assertEqual(candidate['offset'],6);self.assertEqual(candidate['deletion_state'],'unknown')
        export=self.repo.create_task('export')
        export_batch(self.repo,export,[item.id],self.root/'output',self.config.data_dir,CancellationToken())
        record=self.repo.exports(export)[0];self.assertEqual(record['status'],'exported')
        self.assertEqual(record['source_sha256'],record['target_sha256']);self.assertEqual(Path(record['output_path']).read_bytes(),self.pdf)
        self.assertEqual(self.path.read_bytes(),original);self.path.unlink()
        reopened=Repository(self.config.database);self.assertEqual(reopened.candidate_page(task,category='document'),[candidate])
        with self.assertRaises(CandidateAccessError):prepare_pdf(reopened,item.id,self.config.data_dir,CancellationToken())

    def test_computer_partition_absolute_ranges_and_persisted_details(self):
        self.path.write_bytes(b'x'*512+self.pdf+self.pdf)
        with DiskReader(self.path) as reader:
            records=list(ComputerScanner()._signatures(reader,Partition(512,len(self.pdf),'APFS','synthetic'),CancellationToken()))
        self.assertEqual(len(records),1);self.assertEqual(records[0]['extents'],[(512,len(self.pdf))])
        items=list(ComputerScanner(self.config.data_dir).scan(self.path,CancellationToken(),mode='signature'))
        self.assertEqual(len(items),2);self.persist(items,computer=True)
        self.assertEqual(candidate_input(self.repo,items[0].id,self.config.data_dir).offset,512)
        self.assertEqual(items[0].structure_details['pages'],2)

    def test_unverified_candidates_have_no_hash_and_cannot_export(self):
        for data in (self.pdf[:-8],make_pdf(trailer=b'/Encrypt 8 0 R')):
            item=self.scan(data)[0];self.persist([item]);self.assertIsNone(item.sha256)
            with self.assertRaises(CandidateAccessError):candidate_input(self.repo,item.id,self.config.data_dir)

    def test_changed_source_is_rejected_before_renderer(self):
        item=self.scan(self.pdf)[0];self.persist([item]);self.path.write_bytes(self.pdf+b'changed')
        with patch('core.preview.pdf.render_pdf') as renderer:
            with self.assertRaises(CandidateAccessError):prepare_pdf(self.repo,item.id,self.config.data_dir,CancellationToken())
            renderer.assert_not_called()

    @requires_qt
    def test_authorized_android_pdf_is_verified_without_changing_copy_or_metadata(self):
        path=self.config.data_dir/'Acquisitions/synthetic';path.write_bytes(self.pdf)
        source=DataSource('android','synthetic-serial');task=self.repo.create_task('android_acquisition',source)
        item=Candidate('android_file','ordinary.pdf','pdf',len(self.pdf),source_id=source.id,local_path=str(path),source_stat=snapshot(path.stat()),sha256=hashlib.sha256(self.pdf).hexdigest(),integrity='not_checked')
        self.repo.add_candidate(task,item);self.repo.update_task(task,'completed');before=self.repo.candidate(item.id)
        result=prepare_pdf(self.repo,item.id,self.config.data_dir,CancellationToken())
        self.assertEqual(result.pages,2);self.assertEqual(result.data,self.pdf)
        self.assertEqual(path.read_bytes(),self.pdf);self.assertEqual(self.repo.candidate(item.id),before)

    @requires_qt
    def test_ios_extensionless_blob_pdf_uses_verified_local_backup_source(self):
        backup=self.root/'backup';backup.mkdir();path=backup/'0123456789abcdef';path.write_bytes(self.pdf)
        source=DataSource('ios_backup',str(backup));task=self.repo.create_task('ios_manifest',source)
        item=Candidate('ios_backup','Documents/report.pdf','pdf',len(self.pdf),source_id=source.id,local_path=str(path),source_stat=snapshot(path.stat()),integrity='not_checked')
        self.repo.add_candidate(task,item);self.repo.update_task(task,'completed')
        self.assertEqual(prepare_pdf(self.repo,item.id,self.config.data_dir,CancellationToken()).data,self.pdf)
        self.assertEqual(list(backup.iterdir()),[path]);self.assertEqual(path.read_bytes(),self.pdf)

    @requires_qt
    def test_metadata_deleted_pdf_preserves_fragmented_logical_byte_order(self):
        split=300;original=b'prefix'+self.pdf[split:]+b'gap'+self.pdf[:split];self.path.write_bytes(original)
        with DiskReader(self.path) as reader:identity=reader.check()
        extents=((6+len(self.pdf)-split+3,split),(6,len(self.pdf)-split))
        source=DataSource('computer',str(self.path));task=self.repo.create_task('computer_scan',source)
        item=Candidate('computer_deleted','record.pdf','pdf',len(self.pdf),source_id=source.id,source_stat=identity,extents=extents,sha256=hashlib.sha256(self.pdf).hexdigest(),integrity='metadata_checked',deletion_state='deleted_record')
        self.repo.add_candidate(task,item);self.repo.update_task(task,'completed')
        result=prepare_pdf(self.repo,item.id,self.config.data_dir,CancellationToken())
        self.assertEqual(result.data,self.pdf);self.assertEqual(self.path.read_bytes(),original)

    def test_evidence_keeps_deletion_unknown_and_does_not_claim_full_render(self):
        item=self.scan(self.pdf)[0];candidate={'source':item.source,'integrity':item.integrity,'structure_details':item.structure_details}
        self.assertIn('PDF',content_check(item.integrity).label);self.assertIn('未知',deletion_evidence(candidate).label)
        self.assertIn('未渲染全部页面',pdf_check_details(candidate))
        candidate['structure_details']['pages']='<script>';self.assertEqual(pdf_check_details(candidate),'')


@requires_qt
class PdfRenderingTests(unittest.TestCase):
    def test_real_pages_and_zoom_are_rendered_as_distinct_bounded_pixels(self):
        data=make_pdf();token=CancellationToken()
        first=render_pdf(data,0,100,token);second=render_pdf(data,1,100,token);small=render_pdf(data,0,50,token)
        self.assertEqual((first.width(),first.height()),(768,1024))
        self.assertEqual((small.width(),small.height()),(384,512))
        self.assertNotEqual(first.pixelColor(100,500),second.pixelColor(100,500))

    def test_font_resource_named_a_is_not_mistaken_for_an_action(self):
        values={n:v for n,v in objects().items() if n<=5}
        values[2]=b'<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 240 320] >>'
        values[3]=values[3].replace(b'/F1 5 0 R',b'/A 5 0 R')
        values[4]=stream(b'BT /A 18 Tf 20 240 Td (Static font named A) Tj ET')
        data=make_pdf(values)
        self.assertEqual(inspect_pdf(data,CancellationToken())['pages'],1)
        self.assertFalse(render_pdf(data,0,50,CancellationToken()).isNull())

    def test_encrypted_and_active_pdf_are_rejected_before_spawning_renderer(self):
        values=objects();values[1]=values[1][:-2]+b' /OpenAction 8 0 R >>';values[8]=b'null'
        for data in (make_pdf(values),make_pdf(trailer=b'/Encrypt 8 0 R')):
            with self.assertRaises(CandidateAccessError):inspect_pdf(data,CancellationToken())

    def test_render_cancellation_and_resource_limits_reap_the_process(self):
        import subprocess
        real_popen=subprocess.Popen;children=[]
        def opened(*args,**kwargs):
            result=real_popen(*args,**kwargs);children.append(result);return result
        for mode in ('cancel','timeout','memory'):
            with self.subTest(mode=mode):
                token=CancellationToken()
                def memory(process):
                    if mode=='cancel':token.cancel()
                    return MAX_RENDER_RSS+1 if mode=='memory' else 0
                with patch('core.preview.pdf.subprocess.Popen',side_effect=opened),patch('core.preview.pdf.renderer_memory',side_effect=memory),patch('core.preview.pdf.RENDER_TIMEOUT',0 if mode=='timeout' else 8):
                    with self.assertRaises((TaskCancelled,CandidateAccessError)):render_pdf(make_pdf(),0,100,token)
                self.assertIsNotNone(children[-1].poll())

    def test_invalid_worker_protocol_and_oversized_dimensions_are_rejected(self):
        class Process:
            returncode=0
            def poll(self):return 0
            def communicate(self,*args,**kwargs):return self.payload,None
        packets=[b'not JSON\ninvalid',b'{}\ninvalid',b'{"width":200000,"height":200000}\ninvalid',b'{"width":1,"height":1}\n'+b'\x89PNG\r\n\x1a\n'+b'x'*32]
        for packet in packets:
            process=Process();process.payload=packet
            with patch('core.preview.pdf.subprocess.Popen',return_value=process),patch('core.preview.pdf.renderer_memory',return_value=None):
                with self.assertRaises(CandidateAccessError):render_pdf(make_pdf(),0,100,CancellationToken())

    def test_invalid_page_zoom_and_trailing_document_content_are_rejected(self):
        for page,zoom in ((-1,100),(200,100),(0,333)):
            with self.assertRaises(CandidateAccessError):render_pdf(make_pdf(),page,zoom,CancellationToken())
        with self.assertRaises(CandidateAccessError):inspect_pdf(make_pdf()+b'garbage',CancellationToken())
