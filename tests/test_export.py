from dataclasses import replace
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.app_config import AppConfig
from core.candidate_io import CandidateAccessError, candidate_input, read_candidate, snapshot
from core.export.service import export_batch, copy_verified, ExportError
from core.scanner.signature.scanner import SignatureScanner
from core.tasks import CancellationToken, TaskCancelled, TaskManager
from storage.database.repository import Repository, SCHEMA_VERSION
from storage.models.candidate import Candidate
from storage.models.source import DataSource
from tests.test_scanner import png_image, JPEG


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = AppConfig(self.root/'data'); self.config.initialize()
        self.repo = Repository(self.config.database)
        self.manager = TaskManager(self.repo); self.addCleanup(self.manager.shutdown)

    def android(self, data=b'ordinary file', *, name='photo.jpg', extension='jpg', digest=None):
        path = self.config.data_dir/'Acquisitions'/uuid4().hex
        path.write_bytes(data)
        source = DataSource('android','SERIAL','Phone')
        task = self.repo.create_task('android_acquisition',source)
        candidate = Candidate('android_file',name,extension,len(data),original_path='/sdcard/DCIM/'+name,
            local_path=str(path),source_id=source.id,source_stat=snapshot(path.stat()),
            sha256=digest or hashlib.sha256(data).hexdigest(),integrity='not_checked')
        self.repo.add_candidate(task,candidate); self.repo.update_task(task,'completed')
        return candidate, path, task

    def scan(self, data):
        path = self.root/'disk.img'; path.write_bytes(data)
        task = self.manager.submit('signature_scan',lambda token: SignatureScanner().scan(path,token),
            source=DataSource('image',str(path)),persist_candidates=True,batch_size=1)
        task.future.result(5)
        return self.repo.candidate_ids(task.id),path,task.id

    def export(self, ids, destination=None, expected_error=None):
        operation=lambda token,task_id: export_batch(self.repo,task_id,ids,destination or self.root/'output',self.config.data_dir,token)
        if expected_error:
            with self.assertLogs('deeprecover',level='WARNING'):
                task=self.manager.submit('export',operation,with_task_id=True)
                with self.assertRaises(expected_error): task.future.result(5)
        else:
            task=self.manager.submit('export',operation,with_task_id=True)
            task.future.result(5)
        return task

    def test_scan_range_export_matches_exact_bytes_and_preserves_source_and_candidate(self):
        image = png_image(); raw=b'prefix'+image+b'gap'+JPEG+b'end'
        ids,path,source_task = self.scan(raw)
        before=self.repo.candidate_page(source_task)
        task=self.export(ids)
        records=Repository(self.repo.path).exports(task.id)
        self.assertEqual([Path(r['output_path']).read_bytes() for r in records],[image,JPEG])
        self.assertTrue(all(r['status']=='exported' and r['source_sha256']==r['target_sha256'] for r in records))
        self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(self.repo.candidate_page(source_task),before)
        report=json.loads(Path(self.repo.task(task.id)['result_path']).read_text())
        self.assertEqual((report['selected'],report['exported'],report['failed']),(2,2,0))
        self.assertEqual(report['records'][0]['metadata']['offset'],6)
        self.assertEqual(report['records'][0]['metadata']['source_record']['locator'],str(path))

    def test_ordinary_copy_zero_bytes_and_duplicate_selection(self):
        candidate,path,_=self.android(b'',extension='txt')
        task=self.export([candidate.id,candidate.id])
        records=self.repo.exports(task.id)
        self.assertEqual(len(records),1)
        self.assertEqual(Path(records[0]['output_path']).read_bytes(),b'')
        self.assertEqual(records[0]['source_sha256'],hashlib.sha256(b'').hexdigest())
        self.assertEqual(path.read_bytes(),b'')

    def test_repeat_export_and_untrusted_original_names_never_overwrite(self):
        candidate,_,_=self.android(b'bytes',name='../../CON:bad.jpg',extension='../../outside')
        first=self.export([candidate.id]); second=self.export([candidate.id])
        paths=[Path(self.repo.exports(t.id)[0]['output_path']) for t in (first,second)]
        self.assertNotEqual(paths[0].parent,paths[1].parent)
        self.assertTrue(all(p.suffix=='.bin' and p.read_bytes()==b'bytes' for p in paths))
        self.assertFalse((self.root/'outside').exists())

    def test_exclusive_output_creation_preserves_existing_file(self):
        candidate,_,_=self.android()
        target=self.root/'existing'; target.write_bytes(b'keep me')
        item=candidate_input(self.repo,candidate.id,self.config.data_dir)
        with self.assertRaises(FileExistsError): copy_verified(item,target,CancellationToken())
        self.assertEqual(target.read_bytes(),b'keep me')

    def test_output_name_collision_does_not_delete_first_export(self):
        first,_,_=self.android(b'first'); second,_,_=self.android(b'second')
        with patch('core.export.service.uuid4',return_value=type('Id',(),{'hex':'collision'})()):
            task=self.export([first.id,second.id],expected_error=ExportError)
        records=self.repo.exports(task.id)
        self.assertEqual([r['status'] for r in records],['exported','failed'])
        self.assertEqual(Path(records[0]['output_path']).read_bytes(),b'first')

    def test_source_hash_mismatch_cleans_current_file_and_keeps_other_successes(self):
        good,_,_=self.android(b'good'); bad,_,_=self.android(b'bad',digest='0'*64)
        task=self.export([good.id,bad.id],expected_error=ExportError)
        records=self.repo.exports(task.id)
        self.assertEqual([r['status'] for r in records],['exported','failed'])
        self.assertEqual(Path(records[0]['output_path']).read_bytes(),b'good')
        self.assertFalse(Path(records[1]['output_path']).exists())
        self.assertEqual(self.repo.task(task.id)['status'],'failed')
        self.assertEqual(json.loads(Path(self.repo.task(task.id)['result_path']).read_text())['failed'],1)

    def test_target_hash_mismatch_is_not_reported_as_exported(self):
        candidate,_,_=self.android()
        with patch('core.export.service.range_digest',return_value='0'*64):
            task=self.export([candidate.id],expected_error=ExportError)
        record=self.repo.exports(task.id)[0]
        self.assertEqual(record['status'],'failed')
        self.assertFalse(Path(record['output_path']).exists())

    @unittest.skipIf(os.name=='nt','Windows may deny replacement of an open file')
    def test_replaced_target_is_neither_verified_nor_deleted_as_owned_output(self):
        candidate,_,_=self.android(b'source')
        original_open=Path.open
        output_root=(self.root/'output').resolve()
        replaced_paths=[]
        def replacing_open(path,mode='r',*args,**kwargs):
            stream=original_open(path,mode,*args,**kwargs)
            if mode=='rb' and path.parent.parent.resolve()==output_root:
                alternate=path.with_suffix('.replacement')
                with original_open(alternate,'wb') as replacement: replacement.write(b'other!')
                alternate.replace(path)
                replaced_paths.append(path.resolve())
            return stream
        with patch.object(Path,'open',replacing_open):
            task=self.export([candidate.id],expected_error=ExportError)
        record=self.repo.exports(task.id)[0]
        self.assertEqual(replaced_paths,[Path(record['output_path']).resolve()])
        self.assertEqual(record['status'],'failed')
        self.assertEqual(Path(record['output_path']).read_bytes(),b'other!')

    def test_changed_and_truncated_sources_are_rejected(self):
        ids,path,_=self.scan(png_image())
        path.write_bytes(b'changed')
        task=self.export(ids,expected_error=ExportError)
        self.assertEqual(self.repo.exports(task.id)[0]['status'],'failed')
        self.assertFalse(Path(self.repo.exports(task.id)[0]['output_path']).exists())

    def test_change_during_copy_is_detected_and_output_removed(self):
        candidate,path,_=self.android(b'source')
        from core.candidate_io import range_chunks
        def changing(stream,offset,size,token):
            for data in range_chunks(stream,offset,size,token):
                yield data
                path.write_bytes(b'change')
        with patch('core.export.service.range_chunks',side_effect=changing):
            task=self.export([candidate.id],expected_error=ExportError)
        self.assertFalse(Path(self.repo.exports(task.id)[0]['output_path']).exists())

    def test_cancel_during_copy_cleans_current_output_and_writes_manifest(self):
        candidate,_,_=self.android(b'x'*(2*1024*1024))
        from core.candidate_io import range_chunks
        def cancelling(stream,offset,size,token):
            for data in range_chunks(stream,offset,size,token):
                yield data
                token.cancel()
        with patch('core.export.service.range_chunks',side_effect=cancelling):
            task=self.manager.submit('export',lambda token,task_id: export_batch(self.repo,task_id,[candidate.id],self.root/'output',self.config.data_dir,token),with_task_id=True)
            with self.assertRaises(TaskCancelled): task.future.result(5)
        record=self.repo.exports(task.id)[0]
        self.assertEqual(record['status'],'cancelled')
        self.assertFalse(Path(record['output_path']).exists())
        report=json.loads(Path(self.repo.task(task.id)['result_path']).read_text())
        self.assertTrue(report['cancelled']); self.assertEqual(report['exported'],0)

    def test_cancel_between_files_preserves_verified_output_and_unattempted_count(self):
        first,_,_=self.android(b'first'); second,_,_=self.android(b'second')
        captured=[]; finish=self.repo.finish_export
        def operation(token,task_id):
            captured.append(token)
            return export_batch(self.repo,task_id,[first.id,second.id],self.root/'output',self.config.data_dir,token)
        def cancel_after_commit(*args,**kwargs):
            finish(*args,**kwargs)
            if args[1]=='exported': captured[0].cancel()
        with patch.object(self.repo,'finish_export',side_effect=cancel_after_commit):
            task=self.manager.submit('export',operation,with_task_id=True)
            with self.assertRaises(TaskCancelled): task.future.result(5)
        records=self.repo.exports(task.id)
        self.assertEqual([r['status'] for r in records],['exported'])
        self.assertEqual(Path(records[0]['output_path']).read_bytes(),b'first')
        report=json.loads(Path(self.repo.task(task.id)['result_path']).read_text())
        self.assertEqual((report['exported'],report['not_attempted'],report['cancelled']),(1,1,True))

    def test_legacy_scan_is_visible_but_requires_rescan_for_export(self):
        ids,_,source_task=self.scan(png_image())
        stored=self.repo.candidate(ids[0]); stored.pop('task_id')
        stored['id']=str(uuid4()); stored['sha256']=None; stored['source_stat']=None
        self.repo.add_candidate(source_task,Candidate(**stored))
        task=self.export([stored['id']],expected_error=ExportError)
        self.assertEqual(self.repo.candidate_count(source_task),2)
        self.assertEqual(self.repo.exports(task.id)[0]['error_code'],'CandidateAccessError')

    def test_partial_invalid_and_missing_candidates_cannot_be_exported(self):
        ids,_,_=self.scan(png_image()[:-3]+b'gap'+b'\xff\xd8\xff\xd9')
        task=self.export(ids,expected_error=ExportError)
        self.assertTrue(all(r['status']=='failed' for r in self.repo.exports(task.id)))

    def test_ios_blob_export_checks_binding_and_protects_backup_directory(self):
        backup=self.root/'backup'; backup.mkdir(); blob=backup/'blob'
        blob.write_bytes(JPEG)
        source=DataSource('ios_backup',str(backup))
        task=self.repo.create_task('ios_manifest',source)
        candidate=Candidate('ios_backup','original.jpg','jpg',len(JPEG),local_path=str(blob),source_id=source.id,
            source_stat=snapshot(blob.stat()),integrity='not_checked')
        self.repo.add_candidate(task,candidate); self.repo.update_task(task,'completed')
        rejected=self.export([candidate.id],destination=backup/'exports',expected_error=ExportError)
        self.assertIsNone(self.repo.task(rejected.id)['result_path'])
        self.assertEqual(list(backup.iterdir()),[blob])
        exported=self.export([candidate.id])
        self.assertEqual(Path(self.repo.exports(exported.id)[0]['output_path']).read_bytes(),JPEG)
        self.assertEqual(blob.read_bytes(),JPEG)

    def test_out_of_root_local_path_and_source_binding_mismatch_rejected(self):
        candidate,path,task=self.android()
        outside=self.root/'outside'; outside.write_bytes(path.read_bytes())
        for replacement in (replace(candidate,id=str(uuid4()),local_path=str(outside)),replace(candidate,id=str(uuid4()),source_id='wrong')):
            self.repo.add_candidate(task,replacement)
            with self.assertRaises(CandidateAccessError): candidate_input(self.repo,replacement.id,self.config.data_dir)
        rejected=self.export([candidate.id],destination=self.config.data_dir/'Acquisitions',expected_error=ExportError)
        self.assertEqual(self.repo.export_count(rejected.id),0)

    def test_filter_count_page_and_ids_use_the_same_predicate(self):
        task=self.repo.create_task('test')
        candidates=[Candidate('test',str(i),'PNG' if i%2 else 'pdf',1,status='partial' if i<5 else 'candidate') for i in range(205)]
        self.repo.add_candidates(task,candidates)
        filters={'category':'image','status':'candidate'}
        ids=self.repo.candidate_ids(task,**filters)
        pages=self.repo.candidate_page(task,limit=100,**filters)+self.repo.candidate_page(task,offset=100,limit=100,**filters)
        self.assertEqual(len(ids),self.repo.candidate_count(task,**filters))
        self.assertEqual(ids,[c['id'] for c in pages])
        self.assertEqual(self.repo.candidate_count(task,category='other'),0)
        with self.assertRaises(ValueError): self.repo.candidate_page(task,category='injection')
        with self.assertRaises(ValueError): self.repo.candidate_ids(task,limit=1)

    def test_recover_interrupted_export_preserves_audit_status(self):
        candidate,_,_=self.android()
        task=self.repo.create_task('export'); self.repo.update_task(task,'running')
        self.repo.start_export(task,candidate.id,self.root/'partial',{'size':candidate.size,'name':candidate.name})
        reopened=Repository(self.repo.path); reopened.recover_interrupted()
        self.assertEqual(reopened.task(task)['status'],'interrupted')
        self.assertEqual(reopened.exports(task)[0]['status'],'interrupted')

    def test_legacy_android_without_stat_can_still_verify_existing_hash(self):
        candidate,_,task=self.android(JPEG)
        old=replace(candidate,id=str(uuid4()),source_stat=None)
        self.repo.add_candidate(task,old)
        self.assertEqual(read_candidate(self.repo,old.id,self.config.data_dir,CancellationToken()),JPEG)

    def test_read_bounds_and_changed_source_hash_rejected_for_preview(self):
        candidate,_,_=self.android(JPEG)
        with self.assertRaises(CandidateAccessError): read_candidate(self.repo,candidate.id,self.config.data_dir,CancellationToken(),max_bytes=1)
        bad,_,_=self.android(JPEG,digest='0'*64)
        with self.assertRaises(CandidateAccessError): read_candidate(self.repo,bad.id,self.config.data_dir,CancellationToken())

    def test_v2_migration_keeps_sources_and_adds_export_audit(self):
        path=self.root/'v2.db'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE sources(id TEXT PRIMARY KEY,kind TEXT,locator TEXT,label TEXT,created_at TEXT)')
            db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,kind TEXT,status TEXT,created_at TEXT,updated_at TEXT,error_code TEXT,source_id TEXT REFERENCES sources(id))')
            db.execute('CREATE TABLE candidates(id TEXT PRIMARY KEY,task_id TEXT REFERENCES tasks(id),payload TEXT)')
            db.execute("INSERT INTO sources VALUES('source','image','disk.img','Disk','date')")
            db.execute("INSERT INTO tasks VALUES('task','signature_scan','completed','date','date',NULL,'source')")
            db.execute('PRAGMA user_version=2')
        migrated=Repository(path)
        self.assertEqual(migrated.source('source')['locator'],'disk.img')
        self.assertIsNone(migrated.task('task')['result_path'])
        self.assertEqual(migrated.exports('task'),[])
        with closing(sqlite3.connect(path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
