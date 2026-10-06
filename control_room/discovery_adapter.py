"""Safe Control Room adapter around the existing Discovery v2 engine."""
import csv
import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

from discovery_v2.export import HEADERS, build_rows, write_csv
from discovery_v2.models import Config
from discovery_v2.runner import run as run_discovery
from discovery_v2.search import SerperSearch
from discovery_v2.store import Store, read_manifest


class DiscoveryAdapterError(RuntimeError):
    pass


def _safe_cell(value):
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


class DiscoveryV2JobAdapter:
    name = 'DISCOVERY_V2'

    def __init__(self, repository, storage_root, canonical_db, *, environ=None,
                 provider_factory=SerperSearch, runner=run_discovery,
                 export_builder=build_rows, export_writer=write_csv, poll_interval=0.5,
                 max_companies=10):
        self.repository = repository
        self.storage_root = Path(storage_root).expanduser().absolute()
        self.canonical_db = Path(canonical_db).expanduser().absolute()
        self.environ = os.environ if environ is None else environ
        self.provider_factory = provider_factory
        self.runner = runner
        self.export_builder = export_builder
        self.export_writer = export_writer
        self.poll_interval = poll_interval
        self.max_companies = int(max_companies)
        self.worker_id = None

    def _job_directory(self, job_id):
        root = self.storage_root/'jobs'
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.storage_root.is_symlink() or root.is_symlink():
            raise DiscoveryAdapterError('Control Room storage may not use symbolic links')
        path = (root/job_id).absolute()
        if root not in path.parents:
            raise DiscoveryAdapterError('Invalid generated job path')
        path.mkdir(mode=0o700,exist_ok=True)
        if path.is_symlink():
            raise DiscoveryAdapterError('Job directory may not be a symbolic link')
        return path

    def _record_artifact(self, job, path, kind):
        resolved = path.resolve(strict=True)
        root = self.storage_root.resolve()
        if not resolved.is_relative_to(root):
            raise DiscoveryAdapterError('Artifact escaped Control Room storage')
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        self.repository.add_artifact(job['job_id'],kind,str(resolved.relative_to(root)),
                                     resolved.stat().st_size,digest)

    def _write_manifest(self, job, ids, directory):
        path = directory/'manifest.csv'
        if path.exists():
            with path.open(newline='',encoding='utf-8') as handle:
                stored = [int(row['company_id']) for row in csv.DictReader(handle)]
            if stored != ids:
                raise DiscoveryAdapterError('Persisted job manifest does not match selected job items')
        else:
            with path.open('x',newline='',encoding='utf-8') as handle:
                writer = csv.DictWriter(handle,fieldnames=['position','company_id'])
                writer.writeheader()
                writer.writerows({'position':position,'company_id':company_id}
                                 for position,company_id in enumerate(ids))
            os.chmod(path,0o600)
        self._record_artifact(job,path,'MANIFEST')
        return path

    def _ensure_run(self, job, ids, results_path):
        configured = Config(use_municipality=True,max_search_queries_per_company=1)
        if results_path.exists():
            store = Store(results_path)
            runs = store.conn.execute('SELECT run_id FROM discovery_runs ORDER BY created_at').fetchall()
            if len(runs) != 1:
                store.close()
                raise DiscoveryAdapterError('Existing results database does not contain exactly one run')
            run_id = runs[0]['run_id']
            run_row, config = store.validate_run(run_id)
            actual = [row['company_id'] for row in store.conn.execute(
                'SELECT company_id FROM discovery_run_companies WHERE run_id=? ORDER BY manifest_position',(run_id,))]
            if actual != ids or config != configured:
                store.close()
                raise DiscoveryAdapterError('Existing Discovery run does not match the immutable job manifest')
            if job.get('discovery_run_id') and job['discovery_run_id'] != run_id:
                store.close()
                raise DiscoveryAdapterError('Control Room and Discovery run IDs disagree')
            self.repository.set_discovery_run(job['job_id'],run_id)
            return store,run_id
        if job.get('discovery_run_id'):
            raise DiscoveryAdapterError('Discovery run is recorded but its results database is missing')
        descriptor,companies = read_manifest(self.canonical_db,ids,'control-room-canonical')
        store = Store(results_path,source=self.canonical_db,create=True,require_new=True)
        try:
            run_id = store.create_run(descriptor,companies,configured)
            self.repository.set_discovery_run(job['job_id'],run_id)
            return store,run_id
        except BaseException:
            store.close(); raise

    @staticmethod
    def _counts(store,run_id):
        counts = {key:0 for key in ('PENDING','RUNNING','COMPLETED','PARTIAL','FAILED','INELIGIBLE')}
        for row in store.conn.execute('''SELECT status,COUNT(*) count FROM discovery_run_companies
            WHERE run_id=? GROUP BY status''',(run_id,)):
            counts[row['status']] = row['count']
        result = store.conn.execute('''SELECT
            SUM(CASE WHEN website_status IN ('VERIFIED','HIGH','MEDIUM') THEN 1 ELSE 0 END),
            SUM(default_email_contact_id IS NOT NULL),SUM(default_phone_contact_id IS NOT NULL)
            FROM discovery_company_results r JOIN discovery_run_companies c
            ON c.run_id=r.run_id AND c.company_id=r.company_id AND c.selected_attempt_id=r.attempt_id
            WHERE r.run_id=?''',(run_id,)).fetchone()
        counts.update(websites=result[0] or 0,emails=result[1] or 0,phones=result[2] or 0)
        return counts

    def _sync(self, results_path, run_id, callback):
        try:
            store = Store(results_path)
            try:
                callback(self._counts(store,run_id))
            finally:
                store.close()
        except (OSError,sqlite3.Error,ValueError):
            pass

    def _reconciliation(self, job, export_rows, output):
        items = self.repository.job_items(job['job_id'])
        if not items:
            raise DiscoveryAdapterError('Upload job has no persisted items')
        upload = self.repository.get_upload(items[0]['upload_id'])
        source_rows = {row['row_number']:row for row in self.repository.upload_rows(upload['upload_id'])}
        enriched = {row['company_id']:row for row in export_rows}
        metadata = ['control_match_status','control_match_method','canonical_company_id',
                    'canonical_company_name','control_selected','control_exclusion_reason']
        output_headers = [_safe_cell(header) for header in upload['headers']]+metadata+list(HEADERS)
        with output.open('w',newline='',encoding='utf-8-sig') as handle:
            writer = csv.writer(handle); writer.writerow(output_headers)
            for item in items:
                source = source_rows[item['upload_row_number']]
                discovery = enriched.get(item['company_id'],{}) if item['company_id'] is not None else {}
                reason = '' if item['match_status'] == 'MATCHED' else item['match_method'] or item['match_status']
                writer.writerow([_safe_cell(value) for value in source['original_values']] + [
                    item['match_status'],item['match_method'] or '',
                    '' if item['company_id'] is None else item['company_id'],
                    discovery.get('company_name',''),item['selected'],reason] +
                    [discovery.get(header,'') for header in HEADERS])
        os.chmod(output,0o600)

    def run(self, job, progress):
        ids = self.repository.selected_company_ids(job['job_id'])
        outcome, export_rows, _, _ = self.run_subset(job, ids,
            lambda counts: self.repository.update_discovery_progress(
                job['job_id'], counts, self.worker_id))
        directory = self._job_directory(job['job_id'])
        reconciliation = directory/'enriched_upload.csv'
        self._reconciliation(job,export_rows,reconciliation)
        self._record_artifact(job,reconciliation,'UPLOAD_RECONCILIATION')
        return outcome['status']

    def run_subset(self, job, ids, counts_callback):
        """Run the existing engine for an explicit immutable company subset."""
        if not ids or len(ids) != len(set(ids)) or any(type(value) is not int or value < 0 for value in ids):
            raise DiscoveryAdapterError('Invalid or empty immutable Discovery manifest')
        if len(ids) > self.max_companies:
            raise DiscoveryAdapterError(
                f'Live Discovery is limited to {self.max_companies} companies; {len(ids)} were selected')
        directory = self._job_directory(job['job_id'])
        self._write_manifest(job,ids,directory)
        results = directory/'results.sqlite3'
        if not results.exists() and not self.environ.get('SERPER_API_KEY'):
            raise DiscoveryAdapterError('SERPER_API_KEY is required for live Discovery')
        store,run_id = self._ensure_run(job,ids,results)
        store.close()
        self._record_artifact(job,results,'DISCOVERY_RESULTS_DB')
        self._sync(results,run_id,counts_callback)
        stop = threading.Event()
        monitor = threading.Thread(target=lambda: self._monitor(
            stop,results,run_id,counts_callback),daemon=True)
        monitor.start()
        try:
            active = Store(results)
            try:
                run_status = active.conn.execute(
                    'SELECT status FROM discovery_runs WHERE run_id=?', (run_id,)).fetchone()['status']
                if run_status == 'COMPLETED':
                    outcome = {'status': 'COMPLETED', 'run_id': run_id}
                else:
                    key = self.environ.get('SERPER_API_KEY')
                    if not key:
                        raise DiscoveryAdapterError('SERPER_API_KEY is required for live Discovery')
                    outcome = self.runner(active,run_id,provider=self.provider_factory(key))
            finally:
                active.close()
        finally:
            stop.set(); monitor.join(timeout=2)
        self._sync(results,run_id,counts_callback)
        self._record_artifact(job,results,'DISCOVERY_RESULTS_DB')
        export_rows = self.export_builder(self.canonical_db,[results],ids,
            run_ids={str(results.resolve()):run_id})
        discovery_csv = directory/'companies.csv'
        self.export_writer(discovery_csv,export_rows,[self.canonical_db,results])
        self._record_artifact(job,discovery_csv,'DISCOVERY_EXPORT')
        return outcome, export_rows, results, run_id

    def _monitor(self,stop,results,run_id,counts_callback):
        while not stop.wait(self.poll_interval):
            self._sync(results,run_id,counts_callback)
