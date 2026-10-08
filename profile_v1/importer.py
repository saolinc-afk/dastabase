"""Read-only, explicit Discovery v2 evidence binding and import."""
import hashlib, json, sqlite3, tempfile
from contextlib import contextmanager
from pathlib import Path
from discovery.ownership import in_scope
from discovery_v2.store import APPLICATION_ID as DISCOVERY_APPLICATION_ID, SCHEMA_VERSION as DISCOVERY_SCHEMA_VERSION
from profile_v1.blocks import extract_blocks
from profile_v1.store import encode, uid

USABLE = {'VERIFIED','HIGH','MEDIUM'}


def readonly(path):
    path = Path(path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, isolation_level=None, timeout=.2)
    conn.row_factory = sqlite3.Row; conn.execute('PRAGMA query_only=ON')
    return path, conn


def file_hash(path):
    with Path(path).open('rb') as handle: return hashlib.file_digest(handle, 'sha256').hexdigest()


@contextmanager
def frozen_snapshot(path,expected_sha256):
    """Yield a private byte-for-byte source snapshot only when it matches the manifest."""
    resolved=Path(path).expanduser().resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='profile-source-') as directory:
        snapshot=Path(directory)/'discovery.sqlite3'; digest=hashlib.sha256()
        with resolved.open('rb') as source, snapshot.open('xb') as target:
            while chunk:=source.read(1024*1024):
                digest.update(chunk); target.write(chunk)
        if digest.hexdigest()!=expected_sha256: raise ValueError('Discovery source hash mismatch')
        yield resolved,snapshot


def discovery_manifest(path, run_id, company_ids):
    resolved, conn = readonly(path)
    try:
        if (conn.execute('PRAGMA application_id').fetchone()[0] != DISCOVERY_APPLICATION_ID or
                conn.execute('PRAGMA user_version').fetchone()[0] != DISCOVERY_SCHEMA_VERSION):
            raise ValueError('Incompatible Discovery v2 database')
        run = conn.execute('SELECT * FROM discovery_runs WHERE run_id=?',(run_id,)).fetchone()
        if run is None: raise ValueError('Unknown Discovery run')
        rows=[]
        for company_id in company_ids:
            row=conn.execute('SELECT * FROM discovery_run_companies WHERE run_id=? AND company_id=?',(run_id,company_id)).fetchone()
            if row is None or not row['selected_attempt_id']: raise ValueError(f'Company {company_id} has no selected Discovery attempt')
            result=conn.execute('SELECT * FROM discovery_company_results WHERE run_id=? AND company_id=? AND attempt_id=?',(run_id,company_id,row['selected_attempt_id'])).fetchone()
            if result is None: raise ValueError(f'Company {company_id} has no selected Discovery result')
            identity=json.loads(row['identity_snapshot_json'])
            if identity.get('id') != company_id: raise ValueError('Discovery manifest identity mismatch')
            rows.append({'identity':identity,'registered_activity':identity.get('registered_activity'),
                         'discovery_attempt_id':row['selected_attempt_id'],'discovery_result_id':result['result_id']})
            rows[-1]['accepted_website'] = (result['official_website']
                                            if result['website_status'] in USABLE else None)
        descriptor={'database_path':str(resolved),'sha256':file_hash(resolved),'run_id':run_id,
                    'engine_version':run['engine_version'],'rule_version':run['rule_version']}
        return descriptor, rows
    finally: conn.close()


def import_company(store, profile_run_id, profile_attempt_id, company, source_path,
                   source_run_id, source_attempt_id, source_result_id):
    context={'run_id':profile_run_id,'attempt_id':profile_attempt_id,'company_id':company['id']}
    profile_run=store.conn.execute('SELECT source_descriptor_json FROM profile_runs WHERE run_id=?',
      (profile_run_id,)).fetchone()
    if profile_run is None: raise ValueError('Unknown Profile run')
    descriptor=json.loads(profile_run['source_descriptor_json'])
    resolved_source=Path(source_path).expanduser().resolve(strict=True)
    if (resolved_source!=Path(descriptor['database_path']).expanduser().resolve(strict=True) or
            source_run_id!=descriptor['run_id']):
        raise ValueError('Discovery source descriptor mismatch')
    expected_sha256=descriptor['sha256']
    imported=[]
    with frozen_snapshot(resolved_source,expected_sha256) as (resolved,snapshot):
        _,conn=readonly(snapshot)
        try:
            with store.conn:
                row=conn.execute('SELECT * FROM discovery_run_companies WHERE run_id=? AND company_id=?',(source_run_id,company['id'])).fetchone()
                if row is None or row['selected_attempt_id'] != source_attempt_id or json.loads(row['identity_snapshot_json']) != company:
                    raise ValueError('Discovery company identity or selected attempt changed')
                result=conn.execute('SELECT * FROM discovery_company_results WHERE result_id=? AND run_id=? AND company_id=? AND attempt_id=?',(source_result_id,source_run_id,company['id'],source_attempt_id)).fetchone()
                if result is None: raise ValueError('Selected Discovery result mismatch')
                scope=result['verified_scope'] if result['website_status'] in USABLE else None
                source_rows=conn.execute('SELECT * FROM discovery_evidence WHERE run_id=? AND company_id=? AND attempt_id=? ORDER BY rowid',(source_run_id,company['id'],source_attempt_id)).fetchall()
                for source in source_rows:
                    kind=source['source_kind']; final=source['final_url']; requested=source['requested_url']
                    fetched=kind=='FETCHED_PAGE'; authoritative_url=final or requested
                    first_party=bool(scope and fetched and authoritative_url and in_scope(authoritative_url,scope))
                    if not fetched and kind!='SEARCH_RESULT': continue
                    payload=json.loads(source['evidence_payload_json']); html=payload.get('html') if fetched else None
                    evidence_id=uid()
                    values={**context,'evidence_id':evidence_id,'source_kind':('DISCOVERY_FETCHED_PAGE' if fetched else 'DISCOVERY_SEARCH_RESULT'),
                        'source_class':('FIRST_PARTY' if first_party else 'CANDIDATE' if fetched else 'SEARCH_SNIPPET'),
                        'requested_url':requested,'final_url':final,'title':source['title'],
                        'snippet_body':source['snippet_body'],'html':html,'content_hash':source['content_hash'],
                        'observed_at':source['observed_at'],'payload_json':encode(payload),
                        'origin_database':str(resolved),'origin_run_id':source_run_id,
                        'origin_attempt_id':source_attempt_id,'origin_evidence_id':source['evidence_id'],
                        'origin_content_hash':source['content_hash']}
                    store.insert('profile_evidence',values); imported.append(values)
                    if first_party:
                        for block in extract_blocks(values):
                            store.insert('profile_content_blocks',{**context,**block,'evidence_id':evidence_id,
                                'source_url':authoritative_url})
                if file_hash(resolved)!=expected_sha256:
                    raise ValueError('Discovery source changed during import')
        finally: conn.close()
    return imported
