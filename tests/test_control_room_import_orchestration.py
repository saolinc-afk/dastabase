"""No-network durable orchestration tests for Import & Enrich."""
import hashlib
import sqlite3
from pathlib import Path

import pytest

from control_room.import_enrich_adapter import ImportEnrichAdapter
from control_room.discovery_adapter import DiscoveryV2JobAdapter
from control_room.repository import JobRepository
from control_room.worker import Worker


def canonical(path):
    conn = sqlite3.connect(path)
    conn.executescript('''
      CREATE TABLE companies_lite(
        id INTEGER PRIMARY KEY,company_name TEXT,registration_number TEXT,
        tax_number TEXT,address TEXT,municipality TEXT,revenue_2025 REAL,
        employees_2025 REAL);
      INSERT INTO companies_lite VALUES
        (1,'ALFA d.o.o.','1001','11111111','Alfa 1','Kranj',10,2),
        (2,'BETA d.o.o.','1002','22222222','Beta 2','Ljubljana',20,3),
        (3,'DVOJNIK d.o.o.','1003','33333333','Prva 3','Celje',30,4),
        (4,'DVOJNIK d.o.o.','1004','44444444','Druga 4','Maribor',40,5);
      CREATE TABLE website_discovery(
        id INTEGER PRIMARY KEY,company_id INTEGER,website TEXT,status TEXT,
        verified_scope TEXT,relationship TEXT);
      INSERT INTO website_discovery VALUES
        (1,1,'https://alfa.si/','VERIFIED','https://alfa.si/','LEGAL_ENTITY');
      CREATE TABLE email_discovery(
        id INTEGER PRIMARY KEY,company_id INTEGER,email TEXT,website TEXT);
      INSERT INTO email_discovery VALUES
        (1,1,'info@alfa.si','https://alfa.si/');
    ''')
    conn.commit()
    conn.close()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discovery_result(path, company_ids, *, usable=True, with_phone=True):
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
      INSERT INTO discovery_runs VALUES ('run','COMPLETED','engine','rules');
    ''')
    for company_id in company_ids:
        attempt = f'a{company_id}'
        conn.execute('INSERT INTO discovery_run_companies VALUES (?,?,?,?)',
                     ('run', company_id, 'COMPLETED', attempt))
        website = f'https://company-{company_id}.si/' if usable else None
        email_id = f'e{company_id}' if usable else None
        phone_id = f'p{company_id}' if usable and with_phone else None
        conn.execute('''INSERT INTO discovery_company_results VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?)''', (f'r{company_id}', 'run', attempt, company_id,
            'HIGH' if usable else 'REVIEW', website, f'o{company_id}' if usable else None,
            '["ev"]' if usable else '[]', email_id, phone_id, 'FOUND' if usable else 'NONE',
            f'2026-01-01T00:00:{company_id:02d}+00:00'))
        if usable:
            conn.execute('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (email_id, 'run', attempt, company_id, 'EMAIL',
                 f'info{company_id}@company-{company_id}.si', f'oe{company_id}', '["oe"]',
                 'ATTRIBUTED', 'contact-rules', '["GENERAL"]'))
            if with_phone:
                conn.execute('INSERT INTO discovery_contacts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (phone_id, 'run', attempt, company_id, 'PHONE', f'+38610000{company_id}',
                     f'op{company_id}', '["op"]', 'ATTRIBUTED', 'contact-rules', '["MAIN"]'))
    conn.commit(); conn.close()


class SelectiveDiscovery:
    def __init__(self, root, *, usable=True, interrupt=False):
        self.root = root
        self.usable = usable
        self.interrupt = interrupt
        self.calls = []
        self.worker_id = None

    def run_subset(self, job, ids, counts_callback):
        self.calls.append(tuple(ids))
        if self.interrupt:
            self.interrupt = False
            raise KeyboardInterrupt()
        path = self.root/f'results-{len(self.calls)}.sqlite3'
        discovery_result(path, ids, usable=self.usable)
        counts_callback({'COMPLETED': len(ids)})
        rows = [{'company_id': company_id,
                 'website_status': 'HIGH' if self.usable else 'REVIEW'}
                for company_id in ids]
        return {'status': 'COMPLETED'}, rows, path, 'run'


def upload(repository, upload_id='u'):
    headers = ['Ime', 'Email', 'Podjetje', 'Davčna']
    rows = [
        ['Ana', 'ana@alfa.si', 'ALFA d.o.o.', '11111111'],
        ['Boris', 'boris@alfa.si', 'ALFA d.o.o.', '11111111'],
        ['Cilka', '', 'BETA d.o.o.', ''],
        ['Dora', '', 'BETA d.o.o.', ''],
        ['Eva', '', 'DVOJNIK d.o.o.', ''],
        ['Filip', 'filip@gmail.com', 'NEZNANO d.o.o.', ''],
    ]
    metadata = {'upload_id': upload_id, 'original_filename': 'registrations.xlsx',
        'relative_path': f'uploads/{upload_id}.xlsx', 'sha256': 'source-sha',
        'size_bytes': 10, 'format': 'XLSX', 'worksheet_name': 'Prijave',
        'headers': headers, 'rows': rows}
    repository.create_upload(metadata)
    return {'person_name': 0, 'email': 1, 'company_name': 2, 'tax_number': 3}


def run(repository, source, job, *, worker_id='import-worker', item_hook=None,
        discovery=None, historical=()):
    discovery = discovery or SelectiveDiscovery(source.parent)
    adapter = ImportEnrichAdapter(repository, source, historical,
        discovery_adapter=discovery, item_hook=item_hook)
    worker = Worker(repository, adapter, worker_id=worker_id,
                    adapters={'IMPORT_ENRICH': adapter})
    assert worker.run_once()
    return repository.get_job(job['job_id'])


def test_import_job_persists_rows_evidence_partitions_and_deduplicated_future_work(tmp_path):
    source = tmp_path/'canonical.db'; canonical(source); before = digest(source)
    repository = JobRepository(tmp_path/'control.db'); repository.initialize()
    mapping = upload(repository)
    job = repository.create_import_enrich_job('u', 'Registrations', mapping)
    assert (job['module'], job['execution_adapter'], job['import_total_rows'],
            job['progress_stage']) == ('IMPORT_ENRICH', 'IMPORT_ENRICH', 6, 'PARSING')
    initial = repository.job_items(job['job_id'])
    assert len(initial) == 6
    assert [item['upload_row_number'] for item in initial] == [2, 3, 4, 5, 6, 7]
    assert all(item['processing_status'] == 'PENDING' for item in initial)

    selective = SelectiveDiscovery(tmp_path)
    completed = run(repository, source, job, discovery=selective)
    assert completed['status'] == 'COMPLETED'
    assert completed['progress_stage'] == 'DISCOVERY'
    assert completed['processed_company_count'] == 6
    assert (completed['import_matched_count'], completed['import_ambiguous_count'],
            completed['import_unresolved_count']) == (4, 1, 1)
    assert (completed['import_resolved_without_ai_count'],
            completed['import_matched_requires_enrichment_count']) == (4, 0)
    assert (completed['import_matched_company_count'],
            completed['import_existing_satisfied_company_count'],
            completed['import_discovery_required_company_count']) == (2, 0, 2)
    assert selective.calls == [(1, 2)]
    assert repository.import_summary(job['job_id']) == {
        'total_rows': 6, 'processed_rows': 6, 'matched': 4, 'ambiguous': 1,
        'unresolved': 1, 'resolved_without_ai': 4,
        'matched_requires_enrichment': 0}

    items = repository.job_items(job['job_id'])
    assert [item['company_id'] for item in items[:4]] == [1, 1, 2, 2]
    assert [item['match_status'] for item in items] == [
        'MATCHED', 'MATCHED', 'MATCHED', 'MATCHED', 'AMBIGUOUS', 'UNRESOLVED']
    assert items[0]['match_method'] == 'TAX_EXACT'
    assert any(value['kind'] == 'TAX_EXACT' for value in items[0]['match_evidence'])
    assert items[4]['company_id'] is None and {3, 4} <= set(
        value for evidence in items[4]['match_evidence']
        for value in evidence['company_ids'])
    assert items[5]['conflicts'] == []
    assert all(item['ai_eligibility'] == 'NOT_EVALUATED' for item in items)
    assert all(item['estimated_cost'] is None and item['actual_cost'] is None for item in items)
    assert repository.import_enrichment_company_ids(job['job_id']) == []
    assert items[0]['enrichment']['official_website'] == 'https://company-1.si/'
    assert items[2]['enrichment']['official_website'] == 'https://company-2.si/'
    assert items[2]['enrichment']['default_email'] == 'info2@company-2.si'
    assert items[2]['enrichment']['default_phone'] == '+386100002'
    assert items[2]['enrichment'] == items[3]['enrichment']
    assert len(repository.upload_rows('u')) == 6
    assert digest(source) == before


def test_interruption_resumes_pending_rows_without_rewriting_completed_items(tmp_path):
    source = tmp_path/'canonical.db'; canonical(source)
    repository = JobRepository(tmp_path/'control.db'); repository.initialize()
    mapping = upload(repository)
    job = repository.create_import_enrich_job('u', 'Interrupted', mapping)
    seen = []

    def interrupt(item, summary):
        seen.append(item['item_position'])
        if len(seen) == 2:
            raise KeyboardInterrupt()

    adapter = ImportEnrichAdapter(repository, source, item_hook=interrupt)
    worker = Worker(repository, adapter, worker_id='dead',
                    adapters={'IMPORT_ENRICH': adapter}, stale_seconds=0)
    with pytest.raises(KeyboardInterrupt):
        worker.run_once()
    assert repository.get_job(job['job_id'])['processed_company_count'] == 2
    first_two = repository.job_items(job['job_id'])[:2]
    original_evidence = [item['match_evidence_json'] for item in first_two]

    conn = sqlite3.connect(repository.path)
    conn.execute("UPDATE control_jobs SET worker_heartbeat_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",
                 (job['job_id'],))
    conn.execute("UPDATE control_workers SET heartbeat_at='2000-01-01T00:00:00+00:00'")
    conn.commit(); conn.close()
    resumed = run(repository, source, job, worker_id='recovery')
    assert resumed['status'] == 'COMPLETED'
    assert resumed['processed_company_count'] == 6
    final = repository.job_items(job['job_id'])
    assert [item['match_evidence_json'] for item in final[:2]] == original_evidence
    assert sum(item['processing_status'] == 'COMPLETED' for item in final) == 6


def test_missing_configured_canonical_database_fails_job_without_fallback(tmp_path):
    repository = JobRepository(tmp_path/'control.db'); repository.initialize()
    mapping = upload(repository)
    job = repository.create_import_enrich_job('u', 'Missing source', mapping)
    result = run(repository, tmp_path/'production-missing.db', job)
    assert result['status'] == 'FAILED'
    assert result['error_code'] == 'IMPORT_ENRICH_ADAPTER_ERROR'
    assert 'production-missing.db' in result['error_message']
    assert not (tmp_path/'production-missing.db').exists()
    assert repository.import_summary(job['job_id'])['processed_rows'] == 0


def test_accepted_historical_discovery_default_contacts_suppress_new_discovery(tmp_path):
    source = tmp_path/'canonical.db'; canonical(source)
    historical = tmp_path/'historical.sqlite3'
    discovery_result(historical, [1, 2], with_phone=False)
    repository = JobRepository(tmp_path/'control.db'); repository.initialize()
    mapping = upload(repository)
    job = repository.create_import_enrich_job('u', 'Historical reuse', mapping)
    selective = SelectiveDiscovery(tmp_path)
    result = run(repository, source, job, discovery=selective, historical=[historical])
    assert result['status'] == 'COMPLETED'
    assert selective.calls == []
    assert result['import_existing_satisfied_company_count'] == 2
    beta = repository.job_items(job['job_id'])[2]
    assert beta['enrichment']['official_website'] == 'https://company-2.si/'
    assert beta['enrichment']['default_email'] == 'info2@company-2.si'
    assert beta['enrichment']['default_phone'] is None


def test_review_or_non_legal_sparrow_does_not_suppress_selective_discovery(tmp_path):
    source = tmp_path/'canonical.db'; canonical(source)
    conn = sqlite3.connect(source)
    conn.execute('INSERT INTO website_discovery VALUES (2,2,?,?,?,?)',
                 ('https://group.example/', 'VERIFIED', 'https://group.example/', 'GROUP'))
    conn.execute('INSERT INTO email_discovery VALUES (2,2,?,?)',
                 ('info@group.example', 'https://group.example/'))
    conn.commit(); conn.close()
    repository = JobRepository(tmp_path/'control.db'); repository.initialize()
    mapping = upload(repository)
    job = repository.create_import_enrich_job('u', 'Rejected legacy', mapping)
    selective = SelectiveDiscovery(tmp_path)
    run(repository, source, job, discovery=selective)
    assert selective.calls == [(1, 2)]


def test_review_discovery_preserves_existing_data_and_routes_missing_for_later_policy(tmp_path):
    source = tmp_path/'canonical.db'; canonical(source)
    repository = JobRepository(tmp_path/'control.db'); repository.initialize()
    mapping = upload(repository)
    job = repository.create_import_enrich_job('u', 'Review outcome', mapping)
    selective = SelectiveDiscovery(tmp_path, usable=False)
    result = run(repository, source, job, discovery=selective)
    assert (result['import_discovery_usable_company_count'],
            result['import_discovery_missing_company_count']) == (0, 2)
    items = repository.job_items(job['job_id'])
    assert items[0]['enrichment']['official_website'] == 'https://alfa.si/'
    assert items[0]['discovery_status'] == 'REVIEW'
    assert items[0]['route_hint'] == 'MATCHED_REQUIRES_ENRICHMENT'
    assert items[2]['enrichment']['official_website'] is None
    assert items[2]['enrichment_status'] == 'MISSING_AFTER_DISCOVERY'
    assert all(item['ai_eligibility'] == 'NOT_EVALUATED' for item in items)


def test_accepted_sparrow_is_persisted_before_selective_discovery(tmp_path):
    source = tmp_path/'canonical.db'; canonical(source)
    repository = JobRepository(tmp_path/'control.db'); repository.initialize()
    mapping = upload(repository)
    job = repository.create_import_enrich_job('u', 'SPARROW reuse', mapping)
    selective = SelectiveDiscovery(tmp_path, interrupt=True)
    adapter = ImportEnrichAdapter(repository, source, discovery_adapter=selective)
    worker = Worker(repository, adapter, worker_id='interrupted',
                    adapters={'IMPORT_ENRICH': adapter})
    with pytest.raises(KeyboardInterrupt):
        worker.run_once()
    items = repository.job_items(job['job_id'])
    assert items[0]['enrichment']['official_website'] == 'https://alfa.si/'
    assert items[0]['enrichment']['sources'] == ['SPARROW']
    assert items[0]['enrichment_status'] == 'NEEDS_DISCOVERY'
    assert items[4]['enrichment'] == {}
    assert items[5]['enrichment'] == {}


def test_import_discovery_resume_reuses_completed_run_without_second_runner_call(tmp_path):
    source = tmp_path/'canonical.db'; canonical(source)
    repository = JobRepository(tmp_path/'control.db'); repository.initialize()
    mapping = upload(repository)
    job = repository.create_import_enrich_job('u', 'Discovery interruption', mapping)
    runner_calls = []

    def complete_then_interrupt(store, run_id, provider):
        runner_calls.append(run_id)
        with store.conn:
            store.conn.execute("UPDATE discovery_run_companies SET status='COMPLETED' "
                               "WHERE run_id=?", (run_id,))
            store.conn.execute("UPDATE discovery_runs SET status='COMPLETED' WHERE run_id=?",
                               (run_id,))
        raise KeyboardInterrupt()

    def review_rows(source_path, result_paths, ids, run_ids=None):
        return [{'company_id': company_id, 'website_status': 'REVIEW'}
                for company_id in ids]

    def write_stub(path, rows, sources):
        path.write_text('review\n')

    discovery = DiscoveryV2JobAdapter(repository, tmp_path/'storage', source,
        environ={'SERPER_API_KEY': 'test-secret'}, provider_factory=lambda key: object(),
        runner=complete_then_interrupt, export_builder=review_rows,
        export_writer=write_stub, poll_interval=.01, max_companies=10)
    adapter = ImportEnrichAdapter(repository, source, discovery_adapter=discovery)
    worker = Worker(repository, adapter, worker_id='dead',
        adapters={'IMPORT_ENRICH': adapter}, stale_seconds=0)
    with pytest.raises(KeyboardInterrupt):
        worker.run_once()
    assert len(runner_calls) == 1
    conn = sqlite3.connect(repository.path)
    conn.execute("UPDATE control_jobs SET worker_heartbeat_at='2000-01-01T00:00:00+00:00' "
                 "WHERE job_id=?", (job['job_id'],))
    conn.execute("UPDATE control_workers SET heartbeat_at='2000-01-01T00:00:00+00:00'")
    conn.commit(); conn.close()

    resumed_adapter = ImportEnrichAdapter(repository, source, discovery_adapter=discovery)
    resumed = Worker(repository, resumed_adapter, worker_id='recovered',
        adapters={'IMPORT_ENRICH': resumed_adapter}, stale_seconds=0)
    assert resumed.run_once()
    assert len(runner_calls) == 1
    result = repository.get_job(job['job_id'])
    assert result['status'] == 'COMPLETED'
    assert result['import_discovery_processed_company_count'] == 2


def test_v3_control_room_migrates_without_losing_existing_discovery_job(tmp_path):
    database = tmp_path/'control-v3.db'
    schema = Path('control_room/schema.sql').read_text()
    schema = schema.replace(
        "module TEXT NOT NULL CHECK(module IN ('DISCOVERY_CONTACTS','IMPORT_ENRICH'))",
        "module TEXT NOT NULL CHECK(module='DISCOVERY_CONTACTS')")
    schema = schema.replace(
        "CHECK(execution_adapter IN ('FAKE','DISCOVERY_V2','IMPORT_ENRICH'))",
        "CHECK(execution_adapter IN ('FAKE','DISCOVERY_V2'))")
    remove_prefixes = (
        'progress_stage TEXT', 'import_total_rows INTEGER', 'import_matched_count INTEGER',
        'import_ambiguous_count INTEGER', 'import_unresolved_count INTEGER',
        'import_resolved_without_ai_count INTEGER',
        'import_matched_requires_enrichment_count INTEGER', 'processing_status TEXT',
        'import_matched_company_count INTEGER',
        'import_existing_satisfied_company_count INTEGER',
        'import_discovery_required_company_count INTEGER',
        'import_discovery_processed_company_count INTEGER',
        'import_discovery_usable_company_count INTEGER',
        'import_discovery_missing_company_count INTEGER',
        'match_evidence_json TEXT', 'conflicts_json TEXT', 'route_hint TEXT',
        'reusable_enrichment INTEGER', 'ai_eligibility TEXT', 'ai_status TEXT',
        'estimated_input_tokens INTEGER', 'estimated_output_tokens INTEGER',
        'estimated_cost REAL', 'actual_input_tokens INTEGER',
        'actual_output_tokens INTEGER', 'actual_cost REAL',
        'enrichment_status TEXT', 'enrichment_json TEXT', 'discovery_status TEXT',
        'CREATE INDEX IF NOT EXISTS job_items_processing',
        'CREATE INDEX IF NOT EXISTS job_items_company',
    )
    lines = [line for line in schema.splitlines()
             if not line.strip().startswith(remove_prefixes)]
    schema = '\n'.join(lines).replace('PRAGMA user_version=5', 'PRAGMA user_version=3')
    conn = sqlite3.connect(database)
    conn.executescript(schema)
    conn.execute('''INSERT INTO control_jobs(job_id,display_name,input_kind,module,status,
        created_at,selected_company_count,execution_adapter) VALUES
        ('existing','Existing Discovery','UPLOAD','DISCOVERY_CONTACTS','COMPLETED',
         '2026-01-01T00:00:00+00:00',1,'DISCOVERY_V2')''')
    conn.execute('''INSERT INTO job_events(job_id,sequence,created_at,level,event_code,message)
        VALUES ('existing',1,'2026-01-01T00:00:00+00:00','INFO','OLD_EVENT','preserved')''')
    conn.commit(); conn.close()

    repository = JobRepository(database)
    repository.initialize()
    existing = repository.get_job('existing')
    assert (existing['module'], existing['execution_adapter'], existing['display_name']) == (
        'DISCOVERY_CONTACTS', 'DISCOVERY_V2', 'Existing Discovery')
    assert repository.events('existing')[0]['event_code'] == 'OLD_EVENT'
    conn = sqlite3.connect(database)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='control_jobs'"
    ).fetchone()[0]
    conn.close()
    assert 'IMPORT_ENRICH' in sql
