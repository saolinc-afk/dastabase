import io
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from werkzeug.datastructures import FileStorage

from control_room.repository import JobRepository
from merlin.service import MerlinImportEnrichService


def csv_upload(name='companies.csv'):
    data = b'Company,Tax\nALFA d.o.o.,11111111\n'
    return FileStorage(stream=io.BytesIO(data), filename=name,
                       content_type='text/csv')


def setup_services(tmp_path):
    repository = JobRepository(tmp_path/'control.sqlite3')
    repository.initialize()
    service = MerlinImportEnrichService(repository, tmp_path/'storage')
    workspace_a = service.create_workspace('Workspace A')['workspace_id']
    workspace_b = service.create_workspace('Workspace B')['workspace_id']
    upload_a = service.ingest_upload(workspace_a, csv_upload('a.csv'))
    upload_b = service.ingest_upload(workspace_b, csv_upload('b.csv'))
    job_a = service.queue_import(workspace_a, upload_a.upload_id, 'A import', {
        'company_name': 0, 'tax_number': 1},
        ('WEBSITE', 'EMAIL', 'PHONE', 'FINANCIALS'))
    job_b = service.queue_import(workspace_b, upload_b.upload_id, 'B import', {
        'company_name': 0, 'tax_number': 1}, ('EMAIL',))
    return repository, service, workspace_a, workspace_b, upload_a, upload_b, job_a, job_b


def test_workspace_scoped_upload_job_and_artifact_reads_fail_closed(tmp_path):
    repository, service, workspace_a, workspace_b, upload_a, upload_b, job_a, job_b = \
        setup_services(tmp_path)
    repository.add_artifact(job_a.job_id, 'UPLOAD_RECONCILIATION',
                            f'jobs/{job_a.job_id}/result.xlsx', 123, 'sha-a')
    repository.add_artifact(job_b.job_id, 'UPLOAD_RECONCILIATION',
                            f'jobs/{job_b.job_id}/result.xlsx', 456, 'sha-b')
    artifact_a = repository.artifacts(job_a.job_id)[0]

    assert service.upload(workspace_a, upload_a.upload_id).upload_id == upload_a.upload_id
    assert service.job(workspace_a, job_a.job_id) == job_a
    assert service.artifact(workspace_a, artifact_a['artifact_id']).sha256 == 'sha-a'
    assert len(service.artifacts(workspace_a, job_a.job_id)) == 1

    assert service.upload(workspace_b, upload_a.upload_id) is None
    assert service.job(workspace_b, job_a.job_id) is None
    assert service.artifact(workspace_b, artifact_a['artifact_id']) is None
    assert service.artifacts(workspace_b, job_a.job_id) == ()
    assert service.upload(workspace_a, 'unknown') is None
    assert service.job(workspace_a, 'unknown') is None
    assert service.artifact(workspace_a, 'unknown') is None

    with pytest.raises(LookupError, match='Upload not found'):
        service.queue_import(workspace_b, upload_a.upload_id, 'forbidden', {})
    with pytest.raises(LookupError, match='Upload not found'):
        service.queue_import(workspace_a, 'unknown', 'unknown', {})


def test_disabled_workspace_scoped_reads_fail_closed_like_unknown(tmp_path):
    repository, service, workspace_a, _, upload_a, _, job_a, _ = setup_services(tmp_path)
    repository.add_artifact(job_a.job_id, 'UPLOAD_RECONCILIATION',
                            f'jobs/{job_a.job_id}/result.xlsx', 123, 'sha-a')
    artifact = repository.artifacts(job_a.job_id)[0]
    conn = sqlite3.connect(repository.path)
    conn.execute("UPDATE workspaces SET status='DISABLED' WHERE workspace_id=?",
                 (workspace_a,))
    conn.commit(); conn.close()

    assert service.upload(workspace_a, upload_a.upload_id) is None
    assert service.job(workspace_a, job_a.job_id) is None
    assert service.artifact(workspace_a, artifact['artifact_id']) is None
    assert service.artifacts(workspace_a, job_a.job_id) == ()
    assert repository.get_upload_for_workspace(workspace_a, upload_a.upload_id) is None
    assert repository.get_job_for_workspace(workspace_a, job_a.job_id) is None
    assert repository.get_artifact_for_workspace(workspace_a, artifact['artifact_id']) is None
    assert repository.artifacts_for_workspace(workspace_a, job_a.job_id) == []


def test_ownership_propagates_and_admin_api_remains_compatible(tmp_path):
    repository, service, workspace_a, _, upload_a, _, job_a, _ = setup_services(tmp_path)
    stored_upload = repository.get_upload(upload_a.upload_id)
    stored_job = repository.get_job(job_a.job_id)
    assert (stored_upload['workspace_id'], stored_upload['origin_surface']) == (
        workspace_a, 'MERLIN')
    assert (stored_job['workspace_id'], stored_job['origin_surface']) == (
        workspace_a, 'MERLIN')

    metadata = {
        'upload_id': 'admin-upload', 'original_filename': 'admin.csv',
        'relative_path': 'uploads/admin.csv', 'sha256': 'admin-sha',
        'size_bytes': 10, 'format': 'CSV', 'worksheet_name': None,
        'headers': ['Company'], 'rows': [['ADMIN d.o.o.']],
    }
    admin_upload = repository.create_upload(metadata)
    admin_job = repository.create_import_enrich_job(
        admin_upload['upload_id'], 'Admin import', {'company_name': 0})
    assert admin_upload['workspace_id'] is None
    assert admin_upload['origin_surface'] == 'CONTROL_ROOM'
    assert admin_job['workspace_id'] is None
    assert admin_job['origin_surface'] == 'CONTROL_ROOM'
    assert repository.get_upload('admin-upload')['original_filename'] == 'admin.csv'
    assert repository.get_job(admin_job['job_id'])['display_name'] == 'Admin import'
    assert repository.get_upload_for_workspace(workspace_a, 'admin-upload') is None
    assert repository.get_job_for_workspace(workspace_a, admin_job['job_id']) is None


def test_schema_rejects_missing_or_cross_workspace_lineage(tmp_path):
    repository, _, workspace_a, workspace_b, upload_a, upload_b, job_a, _ = \
        setup_services(tmp_path)
    metadata = {
        'upload_id': 'bad', 'original_filename': 'bad.csv',
        'relative_path': 'uploads/bad.csv', 'sha256': 'bad', 'size_bytes': 1,
        'format': 'CSV', 'worksheet_name': None, 'headers': ['Company'],
        'rows': [['BAD']],
    }
    with pytest.raises(sqlite3.IntegrityError):
        repository.create_workspace_upload('unknown-workspace', metadata)

    conn = sqlite3.connect(repository.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match='ownership mismatch'):
            conn.execute('''INSERT INTO job_items(job_id,item_position,upload_id,
                upload_row_number,company_id,match_status,selected)
                VALUES (?,?,?,2,NULL,'PENDING',0)''',
                (job_a.job_id, 99, upload_b.upload_id))
        with pytest.raises(sqlite3.IntegrityError, match='ownership is immutable'):
            conn.execute('UPDATE uploads SET workspace_id=? WHERE upload_id=?',
                         (workspace_b, upload_a.upload_id))
        with pytest.raises(sqlite3.IntegrityError, match='ownership is immutable'):
            conn.execute('UPDATE control_jobs SET workspace_id=? WHERE job_id=?',
                         (workspace_b, job_a.job_id))
    finally:
        conn.close()
    assert repository.get_upload_for_workspace(workspace_a, upload_a.upload_id)


def _v6_schema():
    schema = _v7_schema()
    schema = re.sub(r'CREATE TABLE IF NOT EXISTS workspaces \(.*?\);\n\n', '',
                    schema, count=1, flags=re.S)
    ownership = re.compile(
        r",?\n    workspace_id TEXT REFERENCES workspaces\(workspace_id\),?\n"
        r"    origin_surface TEXT NOT NULL DEFAULT 'CONTROL_ROOM'\n"
        r"        CHECK\(origin_surface IN \('CONTROL_ROOM','MERLIN'\)\),?\n"
        r"    CHECK\(origin_surface != 'MERLIN' OR workspace_id IS NOT NULL\),?",
        re.S)
    schema = ownership.sub('', schema)
    return schema.replace('PRAGMA user_version=7', 'PRAGMA user_version=6')


def _v7_schema():
    schema = _v8_schema()
    schema = schema.replace('    requested_outputs_json TEXT,\n', '')
    return schema.replace('PRAGMA user_version=8', 'PRAGMA user_version=7')


def _v8_schema():
    schema = Path('control_room/schema.sql').read_text()
    for column in (
            'progress_completed_units INTEGER', 'progress_total_units INTEGER',
            'progress_stage_started_at TEXT', 'progress_updated_at TEXT'):
        schema = '\n'.join(line for line in schema.splitlines()
                           if not line.strip().startswith(column))
    return schema.replace('PRAGMA user_version=9', 'PRAGMA user_version=8')


def test_v6_migration_preserves_admin_rows_and_adds_ownership(tmp_path):
    path = tmp_path/'legacy.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript(_v6_schema())
    conn.execute('''INSERT INTO uploads(upload_id,original_filename,relative_path,sha256,
        size_bytes,format,created_at,headers_json,row_count,status)
        VALUES ('old-upload','old.csv','uploads/old.csv','sha',3,'CSV','then',
        '["Company"]',1,'MAPPING')''')
    conn.execute('''INSERT INTO upload_rows(upload_id,row_number,original_values_json,
        match_status) VALUES ('old-upload',2,'["ALFA"]','PENDING')''')
    conn.execute('''INSERT INTO control_jobs(job_id,display_name,input_kind,module,status,
        created_at,queued_at,selected_company_count,execution_adapter,progress_stage,
        import_total_rows) VALUES ('old-job','Old','UPLOAD','IMPORT_ENRICH','QUEUED',
        'then','then',1,'IMPORT_ENRICH','PARSING',1)''')
    conn.commit(); conn.close()

    repository = JobRepository(path)
    repository.initialize()
    migrated_upload = repository.get_upload('old-upload')
    migrated_job = repository.get_job('old-job')
    assert (migrated_upload['workspace_id'], migrated_upload['origin_surface']) == (
        None, 'CONTROL_ROOM')
    assert (migrated_job['workspace_id'], migrated_job['origin_surface']) == (
        None, 'CONTROL_ROOM')
    conn = sqlite3.connect(path)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 9
        assert conn.execute('SELECT COUNT(*) FROM upload_rows').fetchone()[0] == 1
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name='workspaces'").fetchone()
    finally:
        conn.close()


def test_ownership_migration_failure_rolls_back_without_advancing_version(tmp_path):
    path = tmp_path/'broken-legacy.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript(_v6_schema())
    # Collide with the v7 index name after columns have been added, forcing a
    # late migration error that must roll the complete ownership change back.
    conn.execute('CREATE TABLE control_jobs_workspace(value TEXT)')
    conn.commit(); conn.close()

    with pytest.raises(sqlite3.OperationalError):
        JobRepository(path).initialize()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 6
        assert 'workspace_id' not in {
            row[1] for row in conn.execute('PRAGMA table_info(control_jobs)')}
        assert 'workspace_id' not in {
            row[1] for row in conn.execute('PRAGMA table_info(uploads)')}
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name='workspaces'").fetchone()
    finally:
        conn.close()


def test_merlin_service_imports_without_control_room_web_surface():
    command = ('import sys; import merlin.service; '
               "assert 'control_room.app' not in sys.modules; "
               "assert 'monitor.metrics' not in sys.modules")
    result = subprocess.run([sys.executable, '-c', command], cwd=Path.cwd(),
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_requested_outputs_persist_canonically_across_service_reload(tmp_path):
    path = tmp_path/'control.sqlite3'
    repository = JobRepository(path); repository.initialize()
    service = MerlinImportEnrichService(repository, tmp_path/'storage')
    workspace = service.create_workspace('Workspace')['workspace_id']
    upload = service.ingest_upload(workspace, csv_upload())
    job = service.queue_import(workspace, upload.upload_id, 'Requested',
        {'company_name': 0, 'tax_number': 1}, ('PHONE', 'WEBSITE', 'PHONE'))
    assert job.requested_outputs == ('WEBSITE', 'PHONE')
    assert repository.get_job(job.job_id)['requested_outputs_json'] == '["WEBSITE","PHONE"]'

    reloaded_repository = JobRepository(path); reloaded_repository.initialize()
    reloaded = MerlinImportEnrichService(reloaded_repository, tmp_path/'storage')
    assert reloaded.job(workspace, job.job_id).requested_outputs == ('WEBSITE', 'PHONE')


@pytest.mark.parametrize('outputs', [None, (), ('PROFILE',), ('website',), 'EMAIL'])
def test_merlin_rejects_missing_empty_or_unknown_requested_outputs(tmp_path, outputs):
    repository = JobRepository(tmp_path/'control.sqlite3'); repository.initialize()
    service = MerlinImportEnrichService(repository, tmp_path/'storage')
    workspace = service.create_workspace('Workspace')['workspace_id']
    upload = service.ingest_upload(workspace, csv_upload())
    with pytest.raises(ValueError, match='Requested outputs|requested output'):
        service.queue_import(workspace, upload.upload_id, 'Invalid',
            {'company_name': 0}, outputs)
    assert repository.get_upload(upload.upload_id)['status'] == 'MAPPING'


def test_requested_outputs_are_immutable_after_queue(tmp_path):
    repository = JobRepository(tmp_path/'control.sqlite3'); repository.initialize()
    service = MerlinImportEnrichService(repository, tmp_path/'storage')
    workspace = service.create_workspace('Workspace')['workspace_id']
    upload = service.ingest_upload(workspace, csv_upload())
    job = service.queue_import(workspace, upload.upload_id, 'Immutable',
        {'company_name': 0}, ('WEBSITE',))
    conn = sqlite3.connect(repository.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match='requested outputs are immutable'):
            conn.execute('UPDATE control_jobs SET requested_outputs_json=? WHERE job_id=?',
                         ('["EMAIL"]', job.job_id))
    finally:
        conn.close()
    assert service.job(workspace, job.job_id).requested_outputs == ('WEBSITE',)


def test_v7_migration_preserves_jobs_and_adds_requested_outputs(tmp_path):
    path = tmp_path/'v7.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript(_v7_schema())
    conn.execute('''INSERT INTO control_jobs(job_id,display_name,input_kind,module,status,
        created_at,queued_at,selected_company_count,execution_adapter,progress_stage,
        import_total_rows) VALUES ('old-job','Old','UPLOAD','IMPORT_ENRICH','QUEUED',
        'then','then',1,'IMPORT_ENRICH','PARSING',1)''')
    conn.commit(); conn.close()

    repository = JobRepository(path); repository.initialize(); repository.initialize()
    old = repository.get_job('old-job')
    assert old['requested_outputs_json'] is None
    conn = sqlite3.connect(path)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 9
        assert 'requested_outputs_json' in {
            row[1] for row in conn.execute('PRAGMA table_info(control_jobs)')}
    finally:
        conn.close()


def test_v8_migration_preserves_jobs_and_adds_persisted_progress(tmp_path):
    path = tmp_path/'v8.sqlite3'
    conn = sqlite3.connect(path)
    conn.executescript(_v8_schema())
    conn.execute('''INSERT INTO control_jobs(job_id,display_name,input_kind,module,status,
        created_at,queued_at,selected_company_count,execution_adapter,progress_stage,
        import_total_rows,requested_outputs_json) VALUES
        ('old-job','Old','UPLOAD','IMPORT_ENRICH','QUEUED','then','then',3,
         'IMPORT_ENRICH','PARSING',3,'["WEBSITE"]')''')
    conn.commit(); conn.close()

    repository = JobRepository(path); repository.initialize(); repository.initialize()
    old = repository.get_job('old-job')
    assert old['display_name'] == 'Old'
    assert old['requested_outputs_json'] == '["WEBSITE"]'
    assert old['progress_completed_units'] is None
    assert old['progress_total_units'] is None
    conn = sqlite3.connect(path)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 9
        columns = {row[1] for row in conn.execute('PRAGMA table_info(control_jobs)')}
        assert {'progress_completed_units', 'progress_total_units',
                'progress_stage_started_at', 'progress_updated_at'} <= columns
    finally:
        conn.close()
