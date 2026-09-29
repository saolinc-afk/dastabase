"""No-network tests for the Control Room Discovery v2 adapter."""
import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control_room.app import create_app
from control_room.discovery_adapter import DiscoveryV2JobAdapter
from control_room.fake import FakeEnrichmentAdapter
from control_room.repository import JobRepository
from control_room.worker import Worker
from discovery_v2.export import HEADERS, write_csv
from discovery_v2.store import Store


def canonical(path):
    conn=sqlite3.connect(path)
    conn.executescript('''CREATE TABLE companies_lite(id INTEGER PRIMARY KEY,company_name TEXT,
      registration_number TEXT,tax_number TEXT,address TEXT,municipality TEXT,
      revenue_2025 REAL,employees_2025 REAL);
      INSERT INTO companies_lite VALUES
      (0,'ZERO d.o.o.','100','200','Zero 1','Kranj',1,1),
      (1,'ONE d.o.o.','101','201','One 1','Ljubljana',2,2),
      (2,'TWO d.o.o.','102','202','Two 1','Celje',3,3);''')
    conn.close()


class Provider:
    name='fake-serper'; staged=True
    def __init__(self,key): self.key=key


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.source=self.root/'canonical.db'; canonical(self.source); self.before=self.source.read_bytes()
        self.repo=JobRepository(self.root/'control.db'); self.repo.initialize()

    def tearDown(self): self.temp.cleanup()

    def job(self,ids=(0,2),adapter='DISCOVERY_V2'):
        upload_id='u'+str(len(self.repo.list_jobs()))
        metadata={'upload_id':upload_id,'original_filename':'input.csv',
          'relative_path':f'uploads/{upload_id}.csv','sha256':'x','size_bytes':1,'format':'CSV',
          'worksheet_name':None,'headers':['Name'],'rows':[[f'row-{n}'] for n in range(len(ids)+1)]}
        upload=self.repo.create_upload(metadata)
        normalized=[]; matches=[]
        for offset,company_id in enumerate(ids,2):
            normalized.append({'row_number':offset,'normalized_name':'x','normalized_tax_number':'',
              'normalized_registration_number':'','normalized_address':'','normalized_municipality':''})
            matches.append(('MATCHED',[{'company_id':company_id,'rank':1,'match_method':'TAX_EXACT',
              'similarity':None,'reasons':[]}]))
        # Last source row is unresolved, and duplicate IDs remain as separate job items.
        normalized.append({'row_number':len(ids)+2,'normalized_name':'none','normalized_tax_number':'',
          'normalized_registration_number':'','normalized_address':'','normalized_municipality':''})
        matches.append(('NOT_FOUND',[]))
        self.repo.save_mapping_and_matches(upload['upload_id'],{'company_name':0},normalized,matches)
        return self.repo.create_upload_job(upload['upload_id'],'Live job',adapter,10,adapter=='DISCOVERY_V2')

    @staticmethod
    def complete_runner(store,run_id,provider):
        with store.conn:
            store.conn.execute("UPDATE discovery_run_companies SET status='COMPLETED' WHERE run_id=?",(run_id,))
            store.conn.execute("UPDATE discovery_runs SET status='COMPLETED' WHERE run_id=?",(run_id,))
        return {'status':'COMPLETED','run_id':run_id,'processed':2,'failed':0,'partial':0,'remaining':0}

    @staticmethod
    def export_rows(source,results,ids,run_ids=None):
        names={0:'ZERO d.o.o.',1:'ONE d.o.o.',2:'TWO d.o.o.'}
        return [{**{header:'' for header in HEADERS},'company_id':company_id,
                 'company_name':names[company_id],'website_status':'REVIEW'} for company_id in ids]

    def adapter(self,runner=None,export_writer=write_csv):
        return DiscoveryV2JobAdapter(self.repo,self.root/'storage',self.source,
          environ={'SERPER_API_KEY':'test-secret'},provider_factory=Provider,
          runner=runner or self.complete_runner,export_builder=self.export_rows,
          export_writer=export_writer,poll_interval=.01)

    def run_job(self,job,adapter):
        fake=FakeEnrichmentAdapter(delay=0,sleeper=lambda _:None)
        worker=Worker(self.repo,fake,worker_id='worker',adapters={'FAKE':fake,'DISCOVERY_V2':adapter})
        self.assertTrue(worker.run_once()); return self.repo.get_job(job['job_id'])

    def test_real_job_manifest_fixed_config_artifacts_reconciliation_and_id_zero(self):
        job=self.job((0,0,2)); adapter=self.adapter(); result=self.run_job(job,adapter)
        self.assertEqual((result['status'],result['execution_adapter'],result['selected_company_count']),
                         ('COMPLETED','DISCOVERY_V2',2))
        self.assertTrue(result['discovery_run_id'])
        directory=self.root/'storage/jobs'/job['job_id']
        with (directory/'manifest.csv').open() as handle:
            self.assertEqual([int(row['company_id']) for row in csv.DictReader(handle)],[0,2])
        store=Store(directory/'results.sqlite3')
        try:
            run,config=store.validate_run(result['discovery_run_id'])
            self.assertTrue(config.use_municipality)
            self.assertEqual(config.max_search_queries_per_company,1)
        finally: store.close()
        artifacts={row['artifact_type']:row for row in self.repo.artifacts(job['job_id'])}
        self.assertEqual(set(artifacts),{'MANIFEST','DISCOVERY_RESULTS_DB','DISCOVERY_EXPORT','UPLOAD_RECONCILIATION'})
        with (directory/'enriched_upload.csv').open(encoding='utf-8-sig',newline='') as handle:
            rows=list(csv.reader(handle))
        self.assertEqual(len(rows),5)  # header + every original upload row
        self.assertEqual([row[0] for row in rows[1:]],['row-0','row-1','row-2','row-3'])
        self.assertEqual(self.source.read_bytes(),self.before)

    def test_partial_job_persists_and_displays_both_exports(self):
        def partial_runner(store,run_id,provider):
            rows=store.conn.execute('SELECT company_id FROM discovery_run_companies WHERE run_id=? ORDER BY manifest_position',(run_id,)).fetchall()
            with store.conn:
                store.conn.execute("UPDATE discovery_run_companies SET status='COMPLETED' WHERE run_id=? AND company_id=?",(run_id,rows[0][0]))
                store.conn.execute("UPDATE discovery_run_companies SET status='PARTIAL' WHERE run_id=? AND company_id=?",(run_id,rows[1][0]))
                store.conn.execute("UPDATE discovery_runs SET status='PARTIAL' WHERE run_id=?",(run_id,))
            return {'status':'PARTIAL','run_id':run_id,'processed':2,'failed':0,'partial':1,'remaining':1}
        job=self.job((0,2)); result=self.run_job(job,self.adapter(runner=partial_runner))
        self.assertEqual(result['status'],'PARTIAL')
        artifacts={row['artifact_type']:row for row in self.repo.artifacts(job['job_id'])}
        self.assertIn('DISCOVERY_EXPORT',artifacts)
        self.assertIn('UPLOAD_RECONCILIATION',artifacts)
        app=create_app({'TESTING':True,'CONTROL_DB':self.repo.path,
            'CONTROL_STORAGE_ROOT':self.root/'storage','CANONICAL_DB':self.source,
            'DISCOVERY_V2_PATH':self.root/'none'})
        client=app.test_client(); html=client.get(f'/jobs/{job["job_id"]}').get_data(as_text=True)
        self.assertIn('DISCOVERY EXPORT',html); self.assertIn('ENRICHED UPLOAD',html)
        for kind in ('DISCOVERY_EXPORT','UPLOAD_RECONCILIATION'):
            response=client.get(f'/jobs/{job["job_id"]}/artifacts/{artifacts[kind]["artifact_id"]}')
            self.assertEqual(response.status_code,200); response.close()
        hidden=artifacts['DISCOVERY_RESULTS_DB']
        self.assertEqual(client.get(f'/jobs/{job["job_id"]}/artifacts/{hidden["artifact_id"]}').status_code,404)

    def test_formula_injection_is_escaped(self):
        job=self.job((0,)); upload_id=self.repo.job_items(job['job_id'])[0]['upload_id']
        conn=sqlite3.connect(self.repo.path)
        conn.execute("UPDATE upload_rows SET original_values_json='[\"=HYPERLINK(1)\"]' WHERE upload_id=? AND row_number=2",(upload_id,)); conn.commit(); conn.close()
        self.run_job(job,self.adapter())
        path=self.root/'storage/jobs'/job['job_id']/'enriched_upload.csv'
        self.assertIn("'=HYPERLINK(1)",path.read_text(encoding='utf-8-sig'))

    def test_resume_existing_run_without_duplicate_creation(self):
        job=self.job((0,2)); adapter=self.adapter(runner=lambda *args,**kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
        fake=FakeEnrichmentAdapter(delay=0)
        worker=Worker(self.repo,fake,worker_id='crashed',adapters={'FAKE':fake,'DISCOVERY_V2':adapter},stale_seconds=0)
        with self.assertRaises(KeyboardInterrupt): worker.run_once()
        first=self.repo.get_job(job['job_id']); self.assertEqual(first['status'],'RUNNING')
        conn=sqlite3.connect(self.repo.path); conn.execute("UPDATE control_jobs SET worker_heartbeat_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",(job['job_id'],)); conn.execute("UPDATE control_workers SET heartbeat_at='2000-01-01T00:00:00+00:00'"); conn.commit(); conn.close()
        resumed=self.run_job(first,self.adapter())
        self.assertEqual(resumed['discovery_run_id'],first['discovery_run_id'])
        store=Store(self.root/'storage/jobs'/job['job_id']/'results.sqlite3')
        try: self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM discovery_runs').fetchone()[0],1)
        finally: store.close()

    def test_discovery_and_export_failures_persist_failed(self):
        for label,adapter in (
          ('run',self.adapter(runner=lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('discovery failed')))),
          ('export',self.adapter(export_writer=lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError('export failed'))))):
            with self.subTest(label=label):
                job=self.job((1,)); result=self.run_job(job,adapter)
                self.assertEqual(result['status'],'FAILED')
                self.assertEqual(result['error_code'],'DISCOVERY_V2_ADAPTER_ERROR')

    def test_missing_serper_configuration_fails_before_run_creation(self):
        job=self.job((0,))
        adapter=DiscoveryV2JobAdapter(self.repo,self.root/'storage',self.source,environ={},
            provider_factory=Provider,runner=self.complete_runner,export_builder=self.export_rows)
        result=self.run_job(job,adapter)
        self.assertEqual(result['status'],'FAILED')
        self.assertIn('SERPER_API_KEY is required',result['error_message'])
        self.assertIsNone(result['discovery_run_id'])

    def test_adapter_rechecks_live_company_limit(self):
        job=self.job((0,2))
        adapter=DiscoveryV2JobAdapter(self.repo,self.root/'storage',self.source,
            environ={'SERPER_API_KEY':'test'},provider_factory=Provider,runner=self.complete_runner,
            export_builder=self.export_rows,max_companies=1)
        result=self.run_job(job,adapter)
        self.assertEqual(result['status'],'FAILED')
        self.assertIn('limited to 1',result['error_message'])
        self.assertIsNone(result['discovery_run_id'])

    def test_zero_company_live_job_is_rejected(self):
        metadata={'upload_id':'empty','original_filename':'empty.csv','relative_path':'uploads/empty.csv',
          'sha256':'x','size_bytes':1,'format':'CSV','headers':['Name'],'rows':[['none']]}
        upload=self.repo.create_upload(metadata)
        row={'row_number':2,'normalized_name':'none','normalized_tax_number':'',
             'normalized_registration_number':'','normalized_address':'','normalized_municipality':''}
        self.repo.save_mapping_and_matches(upload['upload_id'],{'company_name':0},[row],[('NOT_FOUND',[])])
        with self.assertRaisesRegex(ValueError,'Select at least one'):
            self.repo.create_upload_job(upload['upload_id'],'empty','DISCOVERY_V2',10,True)

    def test_worker_chooses_persisted_adapter_and_fake_still_works(self):
        fake_job=self.repo.create_job('fake',2); calls=[]
        class Adapter:
            def run(self,job,progress): calls.append(job['execution_adapter']); progress(2,1,1,0); return 'COMPLETED'
        worker=Worker(self.repo,Adapter(),worker_id='w',adapters={'FAKE':Adapter(),'DISCOVERY_V2':Adapter()})
        worker.run_once(); self.assertEqual(calls,['FAKE'])
        self.assertEqual(self.repo.get_job(fake_job['job_id'])['status'],'COMPLETED')


class LiveQueueAndDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.source=self.root/'canonical.db'; canonical(self.source)
        self.config={'TESTING':True,'CONTROL_DB':self.root/'control.db','CONTROL_STORAGE_ROOT':self.root/'storage',
          'CANONICAL_DB':self.source,'DISCOVERY_V2_PATH':self.root/'none','REAL_DISCOVERY_MAX_COMPANIES':1}
        self.app=create_app(self.config); self.client=self.app.test_client(); self.repo=self.app.extensions['control_repository']

    def tearDown(self): self.temp.cleanup()

    def ready_upload(self,count=1):
        metadata={'upload_id':'u','original_filename':'x.csv','relative_path':'uploads/x','sha256':'x',
          'size_bytes':1,'format':'CSV','headers':['Name'],'rows':[[str(i)] for i in range(count)]}
        upload=self.repo.create_upload(metadata); normalized=[]; matches=[]
        for position in range(2,count+2):
            normalized.append({'row_number':position,'normalized_name':'x','normalized_tax_number':'',
              'normalized_registration_number':'','normalized_address':'','normalized_municipality':''})
            matches.append(('MATCHED',[{'company_id':position-2,'rank':1,'match_method':'TAX_EXACT','similarity':None,'reasons':[]}]))
        self.repo.save_mapping_and_matches('u',{'company_name':0},normalized,matches); return upload

    def test_live_confirmation_and_limit(self):
        self.ready_upload(1)
        html=self.client.get('/uploads/u/confirm').get_data(as_text=True)
        self.assertNotIn('live_confirmed',html)
        self.assertIn('START ENRICHMENT',html)
        self.assertIn('Max 1 search query/company',html)
        response=self.client.post('/uploads/u/confirm',data={'display_name':'live','execution_adapter':'DISCOVERY_V2'})
        self.assertEqual(response.status_code,303); self.assertEqual(self.repo.list_jobs()[0]['execution_adapter'],'DISCOVERY_V2')

    def test_real_limit_rejects_without_truncation(self):
        self.ready_upload(2)
        response=self.client.post('/uploads/u/confirm',data={'display_name':'live','execution_adapter':'DISCOVERY_V2'})
        self.assertEqual(response.status_code,400); self.assertIn('limited to 1',response.get_data(as_text=True))
        self.assertEqual(self.repo.list_jobs(),[])

    def test_download_is_confined_and_results_database_is_not_downloadable(self):
        job=self.repo.create_job('fake',1); root=self.root/'storage'; root.mkdir(); good=root/'good.csv'; good.write_text('ok')
        self.repo.add_artifact(job['job_id'],'DISCOVERY_EXPORT','good.csv',2,hashlib.sha256(b'ok').hexdigest())
        artifact=self.repo.artifacts(job['job_id'])[0]
        response=self.client.get(f'/jobs/{job["job_id"]}/artifacts/{artifact["artifact_id"]}')
        self.assertEqual(response.status_code,200); response.close()
        outside=self.root/'outside.csv'; outside.write_text('bad')
        self.repo.add_artifact(job['job_id'],'UPLOAD_RECONCILIATION','../outside.csv',3,'x')
        malicious=next(a for a in self.repo.artifacts(job['job_id']) if a['artifact_type']=='UPLOAD_RECONCILIATION')
        self.assertEqual(self.client.get(f'/jobs/{job["job_id"]}/artifacts/{malicious["artifact_id"]}').status_code,404)
        db=root/'results.sqlite3'; db.write_bytes(b'db')
        self.repo.add_artifact(job['job_id'],'DISCOVERY_RESULTS_DB','results.sqlite3',2,'x')
        hidden=next(a for a in self.repo.artifacts(job['job_id']) if a['artifact_type']=='DISCOVERY_RESULTS_DB')
        self.assertEqual(self.client.get(f'/jobs/{job["job_id"]}/artifacts/{hidden["artifact_id"]}').status_code,404)


if __name__=='__main__': unittest.main()
