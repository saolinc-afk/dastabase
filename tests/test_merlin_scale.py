"""Deterministic 200-row MERLIN scale and progress tests; no network calls."""
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from control_room.import_enrich_adapter import ImportEnrichAdapter
from control_room.repository import JobRepository
from control_room.worker import Worker


OUTPUTS = ('WEBSITE', 'EMAIL', 'PHONE', 'FINANCIALS')


@pytest.fixture(autouse=True)
def no_network():
    with (patch('requests.sessions.Session.request',
                side_effect=AssertionError('network/provider call forbidden')),
          patch('socket.socket.connect',
                side_effect=AssertionError('socket call forbidden'))):
        yield


def canonical(path, count=200):
    conn = sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE companies_lite(
        id INTEGER PRIMARY KEY,company_name TEXT,registration_number TEXT,
        tax_number TEXT,address TEXT,municipality TEXT,revenue_2025 REAL,
        profit_2025 REAL,employees_2025 REAL,assets_2025 REAL,capital_2025 REAL);
      CREATE TABLE website_discovery(
        id INTEGER PRIMARY KEY,company_id INTEGER,website TEXT,status TEXT,
        verified_scope TEXT,relationship TEXT);
      CREATE TABLE email_discovery(
        id INTEGER PRIMARY KEY,company_id INTEGER,email TEXT,website TEXT);
    ''')
    conn.executemany('INSERT INTO companies_lite VALUES (?,?,?,?,?,?,?,?,?,?,?)', [
        (company_id, f'COMPANY {company_id:03d} d.o.o.', f'R{company_id:06d}',
         f'{company_id:08d}', f'Address {company_id}', 'Ljubljana',
         company_id * 1000, company_id * 100, company_id + 1,
         company_id * 2000, company_id * 50)
        for company_id in range(1, count + 1)])
    conn.commit(); conn.close()


def discovery_result(path, company_ids, *, usable_ids=None):
    usable_ids = set(company_ids if usable_ids is None else usable_ids)
    conn = sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE discovery_runs(run_id TEXT PRIMARY KEY,status TEXT,
        engine_version TEXT,rule_version TEXT);
      CREATE TABLE discovery_run_companies(run_id TEXT,company_id INTEGER,status TEXT,
        selected_attempt_id TEXT);
      CREATE TABLE discovery_company_results(result_id TEXT,run_id TEXT,attempt_id TEXT,
        company_id INTEGER,website_status TEXT,official_website TEXT,
        website_observation_id TEXT,website_evidence_ids_json TEXT,
        default_email_contact_id TEXT,default_phone_contact_id TEXT,
        contact_outcome TEXT,completed_at TEXT);
      CREATE TABLE discovery_contacts(contact_id TEXT,run_id TEXT,attempt_id TEXT,
        company_id INTEGER,contact_type TEXT,normalized_value TEXT,
        primary_observation_id TEXT,supporting_observation_ids_json TEXT,
        attribution_status TEXT,rule_version TEXT,roles_json TEXT);
      INSERT INTO discovery_runs VALUES ('scale-run','COMPLETED','engine','rules');
    ''')
    for company_id in company_ids:
        usable = company_id in usable_ids
        attempt = f'a{company_id}'
        conn.execute('INSERT INTO discovery_run_companies VALUES (?,?,?,?)',
                     ('scale-run', company_id, 'COMPLETED', attempt))
        conn.execute('''INSERT INTO discovery_company_results VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (f'r{company_id}', 'scale-run', attempt, company_id,
             'HIGH' if usable else 'REVIEW',
             f'https://company-{company_id}.example/' if usable else None,
             f'ow{company_id}' if usable else None, '["website-evidence"]' if usable else '[]',
             f'e{company_id}' if usable else None, f'p{company_id}' if usable else None,
             'FOUND' if usable else 'NONE', '2026-10-09T00:00:00+00:00'))
        if usable:
            conn.execute('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (f'e{company_id}', 'scale-run', attempt, company_id, 'EMAIL',
                 f'info@company-{company_id}.example', f'oe{company_id}', '["oe"]',
                 'ATTRIBUTED', 'rules', '["GENERAL"]'))
            conn.execute('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (f'p{company_id}', 'scale-run', attempt, company_id, 'PHONE',
                 f'+3861{company_id:07d}', f'op{company_id}', '["op"]',
                 'ATTRIBUTED', 'rules', '["MAIN"]'))
    conn.commit(); conn.close()


class BoundedDiscovery:
    def __init__(self, root, repository, *, batch_size=10, usable_count=None,
                 outcome='COMPLETED'):
        self.root = root
        self.repository = repository
        self.batch_size = batch_size
        self.usable_count = usable_count
        self.outcome = outcome
        self.calls = []
        self.batches = []
        self.progress = []
        self.worker_id = None

    def run_subset(self, job, ids, callback):
        ids = tuple(ids); self.calls.append(ids)
        usable_count = len(ids) if self.usable_count is None else self.usable_count
        usable = ids[:usable_count]
        path = self.root/f'scale-results-{len(self.calls)}.sqlite3'
        discovery_result(path, ids, usable_ids=usable)
        completed = 0
        for offset in range(0, len(ids), self.batch_size):
            batch = ids[offset:offset + self.batch_size]
            self.batches.append(batch); completed += len(batch)
            callback({'COMPLETED': completed})
            stored = self.repository.get_job(job['job_id'])
            self.progress.append((stored['progress_stage'],
                                  stored['progress_completed_units'],
                                  stored['progress_total_units']))
        return ({'status': self.outcome},
                [{'company_id': value,
                  'website_status': 'HIGH' if value in usable else 'REVIEW'}
                 for value in ids], path, 'scale-run')


def create_job(tmp_path, company_ids, outputs=OUTPUTS):
    source = tmp_path/'canonical.sqlite3'; canonical(source)
    repository = JobRepository(tmp_path/'control.sqlite3'); repository.initialize()
    workspace = repository.create_workspace('Scale workspace')
    rows = [[f'COMPANY {company_id:03d} d.o.o.', f'{company_id:08d}']
            for company_id in company_ids]
    repository.create_workspace_upload(workspace['workspace_id'], {
        'upload_id': 'scale-upload', 'original_filename': 'scale.csv',
        'relative_path': 'uploads/scale.csv', 'sha256': 'source-sha',
        'size_bytes': 1, 'format': 'CSV', 'worksheet_name': None,
        'headers': ['Company', 'Tax'], 'rows': rows})
    job = repository.create_import_enrich_job('scale-upload', 'Scale job',
        {'company_name': 0, 'tax_number': 1}, outputs)
    return source, repository, job


def run_job(tmp_path, source, repository, job, *, historical=(), discovery=None,
            item_hook=None):
    adapter = ImportEnrichAdapter(repository, source, historical,
        discovery_adapter=discovery, item_hook=item_hook, storage_root=tmp_path/'storage')
    worker = Worker(repository, adapter, worker_id='scale-worker',
                    adapters={'IMPORT_ENRICH': adapter})
    assert worker.run_once()
    return repository.get_job(job['job_id'])


def test_200_rows_all_existing_knowledge_requires_zero_discovery(tmp_path):
    source, repository, job = create_job(tmp_path, range(1, 201))
    historical = tmp_path/'historical.sqlite3'
    discovery_result(historical, range(1, 201))
    result = run_job(tmp_path, source, repository, job, historical=(historical,))
    assert result['status'] == 'COMPLETED'
    assert result['import_discovery_required_company_count'] == 0
    assert result['import_existing_satisfied_company_count'] == 200
    assert len(repository.job_items(job['job_id'])) == 200
    assert len(repository.artifacts(job['job_id'])) == 1


def test_200_duplicate_rows_plan_only_43_unique_discovery_companies(tmp_path):
    ids = [1 + (offset % 43) for offset in range(200)]
    source, repository, job = create_job(tmp_path, ids)
    discovery = BoundedDiscovery(tmp_path, repository)
    result = run_job(tmp_path, source, repository, job, discovery=discovery)
    assert result['status'] == 'COMPLETED'
    assert discovery.calls == [tuple(range(1, 44))]
    assert [len(batch) for batch in discovery.batches] == [10, 10, 10, 10, 3]
    assert result['import_matched_company_count'] == 43
    assert len(repository.job_items(job['job_id'])) == 200


def test_200_rows_with_43_missing_complete_across_bounded_batches(tmp_path):
    source, repository, job = create_job(tmp_path, range(1, 201))
    historical = tmp_path/'historical.sqlite3'
    discovery_result(historical, range(1, 158))
    discovery = BoundedDiscovery(tmp_path, repository)
    result = run_job(tmp_path, source, repository, job,
                     historical=(historical,), discovery=discovery)
    assert result['status'] == 'COMPLETED'
    assert discovery.calls == [tuple(range(158, 201))]
    assert [len(batch) for batch in discovery.batches] == [10, 10, 10, 10, 3]
    assert discovery.progress == [
        ('DISCOVERY', 10, 43), ('DISCOVERY', 20, 43),
        ('DISCOVERY', 30, 43), ('DISCOVERY', 40, 43),
        ('DISCOVERY', 43, 43)]
    assert result['import_discovery_required_company_count'] == 43
    assert result['import_discovery_processed_company_count'] == 43
    assert result['progress_completed_units'] == result['progress_total_units'] == 200
    assert len(repository.artifacts(job['job_id'])) == 1


def test_incomplete_batch_produces_useful_partial_workbook(tmp_path):
    source, repository, job = create_job(tmp_path, range(1, 201))
    historical = tmp_path/'historical.sqlite3'
    discovery_result(historical, range(1, 158))
    discovery = BoundedDiscovery(tmp_path, repository, usable_count=33, outcome='PARTIAL')
    result = run_job(tmp_path, source, repository, job,
                     historical=(historical,), discovery=discovery)
    assert result['status'] == 'PARTIAL'
    assert result['import_discovery_usable_company_count'] == 33
    assert result['import_discovery_missing_company_count'] == 10
    assert len(repository.artifacts(job['job_id'])) == 1
    items = repository.job_items(job['job_id'])
    assert sum(item['enrichment_status'] == 'MISSING_AFTER_DISCOVERY'
               for item in items) == 10


def test_financials_only_200_rows_never_invokes_discovery(tmp_path):
    source, repository, job = create_job(tmp_path, range(1, 201), ('FINANCIALS',))
    discovery = BoundedDiscovery(tmp_path, repository)
    result = run_job(tmp_path, source, repository, job, discovery=discovery)
    assert result['status'] == 'COMPLETED'
    assert discovery.calls == []
    assert result['import_discovery_required_company_count'] == 0
    assert all(item['enrichment']['revenue_2025'] is not None
               for item in repository.job_items(job['job_id']))


def test_persisted_matching_and_discovery_progress_is_monotonic(tmp_path):
    source, repository, job = create_job(tmp_path, range(1, 201))
    matching = []
    def observe_matching(item, summary):
        stored = repository.get_job(job['job_id'])
        matching.append((stored['progress_stage'], stored['progress_completed_units'],
                         stored['progress_total_units']))
    discovery = BoundedDiscovery(tmp_path, repository)
    result = run_job(tmp_path, source, repository, job, discovery=discovery,
                     item_hook=observe_matching)
    assert matching[0] == ('MATCHING', 1, 200)
    assert matching[-1] == ('MATCHING', 200, 200)
    assert [entry[1] for entry in matching] == list(range(1, 201))
    assert discovery.progress == [
        ('DISCOVERY', completed, 200) for completed in range(10, 201, 10)]
    assert result['status'] == 'COMPLETED'
    assert result['progress_completed_units'] == result['progress_total_units'] == 200
