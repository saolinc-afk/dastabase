"""No-network durable orchestration tests for Import & Enrich."""
import hashlib
import sqlite3
from pathlib import Path

import pytest

from control_room.import_enrich_adapter import ImportEnrichAdapter
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


def run(repository, source, job, *, worker_id='import-worker', item_hook=None):
    adapter = ImportEnrichAdapter(repository, source, item_hook=item_hook)
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

    completed = run(repository, source, job)
    assert completed['status'] == 'COMPLETED'
    assert completed['processed_company_count'] == 6
    assert (completed['import_matched_count'], completed['import_ambiguous_count'],
            completed['import_unresolved_count']) == (4, 1, 1)
    assert (completed['import_resolved_without_ai_count'],
            completed['import_matched_requires_enrichment_count']) == (2, 2)
    assert repository.import_summary(job['job_id']) == {
        'total_rows': 6, 'processed_rows': 6, 'matched': 4, 'ambiguous': 1,
        'unresolved': 1, 'resolved_without_ai': 2,
        'matched_requires_enrichment': 2}

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
    assert repository.import_enrichment_company_ids(job['job_id']) == [2]
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
        'match_evidence_json TEXT', 'conflicts_json TEXT', 'route_hint TEXT',
        'reusable_enrichment INTEGER', 'ai_eligibility TEXT', 'ai_status TEXT',
        'estimated_input_tokens INTEGER', 'estimated_output_tokens INTEGER',
        'estimated_cost REAL', 'actual_input_tokens INTEGER',
        'actual_output_tokens INTEGER', 'actual_cost REAL',
        'CREATE INDEX IF NOT EXISTS job_items_processing',
        'CREATE INDEX IF NOT EXISTS job_items_company',
    )
    lines = [line for line in schema.splitlines()
             if not line.strip().startswith(remove_prefixes)]
    schema = '\n'.join(lines).replace('PRAGMA user_version=4', 'PRAGMA user_version=3')
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
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 4
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='control_jobs'"
    ).fetchone()[0]
    conn.close()
    assert 'IMPORT_ENRICH' in sql
