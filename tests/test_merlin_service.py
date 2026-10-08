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
        'company_name': 0, 'tax_number': 1})
    job_b = service.queue_import(workspace_b, upload_b.upload_id, 'B import', {
        'company_name': 0, 'tax_number': 1})
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
    schema = Path('control_room/schema.sql').read_text()
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
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 7
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
