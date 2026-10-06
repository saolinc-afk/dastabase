"""SQLite-backed Control Room jobs, events and worker heartbeats."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from control_room.states import validate_transition


def now():
    return datetime.now(timezone.utc).isoformat()


class JobRepository:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_job_limit()
        self._migrate_import_enrich()
        conn = self._connect()
        try:
            conn.executescript(Path(__file__).with_name('schema.sql').read_text())
            self._migrate_adapter_columns(conn)
            conn.execute('PRAGMA user_version=4')
        finally:
            conn.close()

    @staticmethod
    def _migrate_adapter_columns(conn):
        columns = {row[1] for row in conn.execute('PRAGMA table_info(control_jobs)')}
        additions = {
            'execution_adapter': "TEXT NOT NULL DEFAULT 'FAKE' CHECK(execution_adapter IN ('FAKE','DISCOVERY_V2'))",
            'discovery_run_id': 'TEXT',
            'completed_company_count': 'INTEGER NOT NULL DEFAULT 0',
            'partial_company_count': 'INTEGER NOT NULL DEFAULT 0',
            'failed_company_count': 'INTEGER NOT NULL DEFAULT 0',
            'ineligible_company_count': 'INTEGER NOT NULL DEFAULT 0',
            'progress_stage': 'TEXT',
            'import_total_rows': 'INTEGER NOT NULL DEFAULT 0',
            'import_matched_count': 'INTEGER NOT NULL DEFAULT 0',
            'import_ambiguous_count': 'INTEGER NOT NULL DEFAULT 0',
            'import_unresolved_count': 'INTEGER NOT NULL DEFAULT 0',
            'import_resolved_without_ai_count': 'INTEGER NOT NULL DEFAULT 0',
            'import_matched_requires_enrichment_count': 'INTEGER NOT NULL DEFAULT 0',
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f'ALTER TABLE control_jobs ADD COLUMN {name} {definition}')

    def _migrate_import_enrich(self):
        """Expand existing Control Room tables without replacing their queue."""
        if not self.path.exists():
            return
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            job_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='control_jobs'"
            ).fetchone()
            if job_sql and ('IMPORT_ENRICH' not in (job_sql[0] or '') or
                            'import_total_rows' not in {row[1] for row in
                                conn.execute('PRAGMA table_info(control_jobs)')}):
                conn.execute('PRAGMA foreign_keys=OFF')
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('''CREATE TABLE control_jobs_import_v4 (
                    job_number INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
                    input_kind TEXT NOT NULL CHECK(input_kind IN ('FAKE','UPLOAD','DASTABASE_SELECTION')),
                    module TEXT NOT NULL CHECK(module IN ('DISCOVERY_CONTACTS','IMPORT_ENRICH')),
                    status TEXT NOT NULL CHECK(status IN ('DRAFT','REVIEW_REQUIRED','QUEUED','STARTING',
                        'RUNNING','EXPORTING','COMPLETED','PARTIAL','FAILED')),
                    created_at TEXT NOT NULL, queued_at TEXT, started_at TEXT, finished_at TEXT,
                    worker_id TEXT, worker_heartbeat_at TEXT,
                    selected_company_count INTEGER NOT NULL CHECK(selected_company_count BETWEEN 1 AND 5000),
                    processed_company_count INTEGER NOT NULL DEFAULT 0 CHECK(processed_company_count >= 0),
                    emails_found INTEGER NOT NULL DEFAULT 0 CHECK(emails_found >= 0),
                    websites_found INTEGER NOT NULL DEFAULT 0 CHECK(websites_found >= 0),
                    phones_found INTEGER NOT NULL DEFAULT 0 CHECK(phones_found >= 0),
                    error_code TEXT, error_message TEXT,
                    execution_adapter TEXT NOT NULL DEFAULT 'FAKE'
                        CHECK(execution_adapter IN ('FAKE','DISCOVERY_V2','IMPORT_ENRICH')),
                    discovery_run_id TEXT, completed_company_count INTEGER NOT NULL DEFAULT 0,
                    partial_company_count INTEGER NOT NULL DEFAULT 0,
                    failed_company_count INTEGER NOT NULL DEFAULT 0,
                    ineligible_company_count INTEGER NOT NULL DEFAULT 0,
                    progress_stage TEXT, import_total_rows INTEGER NOT NULL DEFAULT 0,
                    import_matched_count INTEGER NOT NULL DEFAULT 0,
                    import_ambiguous_count INTEGER NOT NULL DEFAULT 0,
                    import_unresolved_count INTEGER NOT NULL DEFAULT 0,
                    import_resolved_without_ai_count INTEGER NOT NULL DEFAULT 0,
                    import_matched_requires_enrichment_count INTEGER NOT NULL DEFAULT 0,
                    CHECK(processed_company_count <= selected_company_count))''')
                source = [row[1] for row in conn.execute('PRAGMA table_info(control_jobs)')]
                target = {row[1] for row in conn.execute('PRAGMA table_info(control_jobs_import_v4)')}
                columns = ','.join(name for name in source if name in target)
                conn.execute(f'INSERT INTO control_jobs_import_v4({columns}) '
                             f'SELECT {columns} FROM control_jobs')
                conn.execute('DROP TABLE control_jobs')
                conn.execute('ALTER TABLE control_jobs_import_v4 RENAME TO control_jobs')
                conn.commit()
            item_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='job_items'"
            ).fetchone()
            if item_sql:
                columns = {row[1] for row in conn.execute('PRAGMA table_info(job_items)')}
                additions = {
                    'processing_status': "TEXT NOT NULL DEFAULT 'PENDING' CHECK(processing_status IN ('PENDING','COMPLETED'))",
                    'match_evidence_json': "TEXT NOT NULL DEFAULT '[]'",
                    'conflicts_json': "TEXT NOT NULL DEFAULT '[]'",
                    'route_hint': 'TEXT',
                    'reusable_enrichment': 'INTEGER NOT NULL DEFAULT 0 CHECK(reusable_enrichment IN (0,1))',
                    'ai_eligibility': "TEXT NOT NULL DEFAULT 'NOT_EVALUATED'",
                    'ai_status': "TEXT NOT NULL DEFAULT 'NOT_STARTED'",
                    'estimated_input_tokens': 'INTEGER', 'estimated_output_tokens': 'INTEGER',
                    'estimated_cost': 'REAL', 'actual_input_tokens': 'INTEGER',
                    'actual_output_tokens': 'INTEGER', 'actual_cost': 'REAL',
                }
                for name, definition in additions.items():
                    if name not in columns:
                        conn.execute(f'ALTER TABLE job_items ADD COLUMN {name} {definition}')
            conn.execute('PRAGMA user_version=4')
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_job_limit(self):
        """Expand the v1 job bound without discarding existing jobs/events."""
        if not self.path.exists():
            return
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='control_jobs'").fetchone()
            if not row or 'BETWEEN 1 AND 1000' not in (row[0] or ''):
                return
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE control_jobs_v2 (
                job_number INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL, input_kind TEXT NOT NULL CHECK(input_kind IN ('FAKE','UPLOAD','DASTABASE_SELECTION')),
                module TEXT NOT NULL CHECK(module='DISCOVERY_CONTACTS'), status TEXT NOT NULL CHECK(status IN
                ('DRAFT','REVIEW_REQUIRED','QUEUED','STARTING','RUNNING','EXPORTING','COMPLETED','PARTIAL','FAILED')),
                created_at TEXT NOT NULL, queued_at TEXT, started_at TEXT, finished_at TEXT,
                worker_id TEXT, worker_heartbeat_at TEXT,
                selected_company_count INTEGER NOT NULL CHECK(selected_company_count BETWEEN 1 AND 5000),
                processed_company_count INTEGER NOT NULL DEFAULT 0 CHECK(processed_company_count >= 0),
                emails_found INTEGER NOT NULL DEFAULT 0 CHECK(emails_found >= 0),
                websites_found INTEGER NOT NULL DEFAULT 0 CHECK(websites_found >= 0),
                phones_found INTEGER NOT NULL DEFAULT 0 CHECK(phones_found >= 0),
                error_code TEXT, error_message TEXT,
                CHECK(processed_company_count <= selected_company_count))''')
            source_columns = [item[1] for item in conn.execute('PRAGMA table_info(control_jobs)')]
            target_columns = {item[1] for item in conn.execute('PRAGMA table_info(control_jobs_v2)')}
            columns = ','.join(name for name in source_columns if name in target_columns)
            conn.execute(f'INSERT INTO control_jobs_v2({columns}) SELECT {columns} FROM control_jobs')
            conn.execute('DROP TABLE control_jobs')
            conn.execute('ALTER TABLE control_jobs_v2 RENAME TO control_jobs')
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        return conn

    def _event(self, conn, job_id, code, message, level='INFO', details=None):
        sequence = conn.execute(
            'SELECT COALESCE(MAX(sequence),0)+1 FROM job_events WHERE job_id=?',
            (job_id,)).fetchone()[0]
        conn.execute('''INSERT INTO job_events
            (job_id,sequence,created_at,level,event_code,message,details_json)
            VALUES (?,?,?,?,?,?,?)''',
            (job_id, sequence, now(), level, code, message,
             json.dumps(details or {}, sort_keys=True, separators=(',', ':'))))

    def create_job(self, display_name, selected_company_count):
        name = str(display_name or '').strip()
        if not 1 <= len(name) <= 120:
            raise ValueError('Job name must be between 1 and 120 characters')
        if type(selected_company_count) is not int or not 1 <= selected_company_count <= 5000:
            raise ValueError('Fake company count must be between 1 and 5000')
        job_id = uuid.uuid4().hex
        created = now()
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''INSERT INTO control_jobs
                (job_id,display_name,input_kind,module,status,created_at,queued_at,
                 selected_company_count) VALUES (?,?,?,?,?,?,?,?)''',
                (job_id, name, 'FAKE', 'DISCOVERY_CONTACTS', 'QUEUED', created,
                 created, selected_company_count))
            self._event(conn, job_id, 'JOB_QUEUED', 'job queued',
                        details={'selected_company_count': selected_company_count})
            conn.commit()
            return self.get_job(job_id)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_upload(self, metadata):
        created = now()
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''INSERT INTO uploads(upload_id,original_filename,relative_path,sha256,
                size_bytes,format,worksheet_name,created_at,headers_json,row_count,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (metadata['upload_id'], metadata['original_filename'], metadata['relative_path'],
                 metadata['sha256'], metadata['size_bytes'], metadata['format'],
                 metadata.get('worksheet_name'), created,
                 json.dumps(metadata['headers'], ensure_ascii=False), len(metadata['rows']), 'MAPPING'))
            conn.executemany('''INSERT INTO upload_rows(upload_id,row_number,original_values_json,match_status)
                VALUES (?,?,?,'PENDING')''', ((metadata['upload_id'], number,
                json.dumps(values, ensure_ascii=False)) for number, values in enumerate(metadata['rows'], 2)))
            conn.commit()
            return self.get_upload(metadata['upload_id'])
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_upload(self, upload_id):
        conn = self._connect()
        try:
            row = conn.execute('SELECT * FROM uploads WHERE upload_id=?', (upload_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result['headers'] = json.loads(result['headers_json'])
            result['mapping'] = json.loads(result['mapping_json'])
            return result
        finally:
            conn.close()

    def upload_rows(self, upload_id, status=None):
        conn = self._connect()
        try:
            query = 'SELECT * FROM upload_rows WHERE upload_id=?'
            params = [upload_id]
            if status:
                query += ' AND match_status=?'; params.append(status)
            rows = []
            for row in conn.execute(query+' ORDER BY row_number', params):
                value = dict(row); value['original_values'] = json.loads(value['original_values_json'])
                rows.append(value)
            return rows
        finally:
            conn.close()

    def save_mapping_and_matches(self, upload_id, mapping, normalized_rows, matches):
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            upload = conn.execute('SELECT status FROM uploads WHERE upload_id=?', (upload_id,)).fetchone()
            if not upload or upload['status'] not in ('MAPPING','REVIEW'):
                raise ValueError('Upload is not available for matching')
            conn.execute('DELETE FROM match_candidates WHERE upload_id=?', (upload_id,))
            for row, (status, candidates) in zip(normalized_rows, matches):
                selected = 1 if status == 'MATCHED' else 0
                chosen = candidates[0]['company_id'] if selected else None
                method = candidates[0]['match_method'] if candidates else None
                conn.execute('''UPDATE upload_rows SET normalized_name=?,normalized_tax_number=?,
                    normalized_registration_number=?,normalized_address=?,normalized_municipality=?,
                    match_status=?,selected_company_id=?,selected=?,match_method=?
                    WHERE upload_id=? AND row_number=?''',
                    (row['normalized_name'],row['normalized_tax_number'],row['normalized_registration_number'],
                     row['normalized_address'],row['normalized_municipality'],status,chosen,selected,method,
                     upload_id,row['row_number']))
                for candidate in candidates:
                    conn.execute('''INSERT INTO match_candidates(upload_id,row_number,company_id,rank,
                        match_method,similarity,reasons_json,selected) VALUES (?,?,?,?,?,?,?,?)''',
                        (upload_id,row['row_number'],candidate['company_id'],candidate['rank'],
                         candidate['match_method'],candidate['similarity'],
                         json.dumps(candidate['reasons'],ensure_ascii=False),
                         1 if chosen == candidate['company_id'] else 0))
            conn.execute("UPDATE uploads SET mapping_json=?,status='REVIEW' WHERE upload_id=?",
                         (json.dumps(mapping, sort_keys=True),upload_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def candidates(self, upload_id):
        conn = self._connect()
        try:
            result = {}
            for row in conn.execute('''SELECT * FROM match_candidates WHERE upload_id=?
                ORDER BY row_number,rank''', (upload_id,)):
                value = dict(row); value['reasons'] = json.loads(value['reasons_json'])
                result.setdefault(value['row_number'], []).append(value)
            return result
        finally:
            conn.close()

    def resolve_upload_row(self, upload_id, row_number, company_id=None):
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            upload = conn.execute('SELECT status FROM uploads WHERE upload_id=?',(upload_id,)).fetchone()
            if not upload or upload['status'] != 'REVIEW':
                raise ValueError('Upload decisions are closed')
            row = conn.execute('SELECT * FROM upload_rows WHERE upload_id=? AND row_number=?',
                               (upload_id,row_number)).fetchone()
            if not row:
                raise ValueError('Unknown upload row')
            if company_id is None:
                conn.execute('''UPDATE upload_rows SET match_status='EXCLUDED',selected=0,
                    selected_company_id=NULL,match_method='MANUAL_EXCLUDE' WHERE upload_id=? AND row_number=?''',
                    (upload_id,row_number))
                conn.execute('UPDATE match_candidates SET selected=0 WHERE upload_id=? AND row_number=?',
                             (upload_id,row_number))
            else:
                candidate = conn.execute('''SELECT match_method FROM match_candidates
                    WHERE upload_id=? AND row_number=? AND company_id=?''',
                    (upload_id,row_number,company_id)).fetchone()
                if not candidate:
                    raise ValueError('Company is not a candidate for this row')
                conn.execute('''UPDATE upload_rows SET match_status='MATCHED',selected=1,
                    selected_company_id=?,match_method=? WHERE upload_id=? AND row_number=?''',
                    (company_id,'MANUAL_'+candidate['match_method'],upload_id,row_number))
                conn.execute('''UPDATE match_candidates SET selected=CASE WHEN company_id=? THEN 1 ELSE 0 END
                    WHERE upload_id=? AND row_number=?''', (company_id,upload_id,row_number))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upload_summary(self, upload_id):
        conn = self._connect()
        try:
            counts = {row['match_status']: row['count'] for row in conn.execute('''SELECT match_status,
                COUNT(*) count FROM upload_rows WHERE upload_id=? GROUP BY match_status''',(upload_id,))}
            unique = conn.execute('''SELECT COUNT(DISTINCT selected_company_id) FROM upload_rows
                WHERE upload_id=? AND match_status='MATCHED' AND selected=1''',(upload_id,)).fetchone()[0]
            return {'counts': counts, 'selected_unique': unique,
                    'unresolved': counts.get('AMBIGUOUS',0)+counts.get('NOT_FOUND',0)}
        finally:
            conn.close()

    def create_upload_job(self, upload_id, display_name, execution_adapter='FAKE',
                          real_company_limit=10, live_confirmed=False):
        name = str(display_name or '').strip()
        if not 1 <= len(name) <= 120:
            raise ValueError('Job name must be between 1 and 120 characters')
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            upload = conn.execute('SELECT * FROM uploads WHERE upload_id=?',(upload_id,)).fetchone()
            if not upload or upload['status'] != 'REVIEW':
                raise ValueError('Upload is not ready for confirmation')
            rows = conn.execute('''SELECT * FROM upload_rows WHERE upload_id=? AND match_status='MATCHED'
                AND selected=1 ORDER BY row_number''',(upload_id,)).fetchall()
            unique = {}
            for row in rows:
                unique.setdefault(row['selected_company_id'], row)
            if not unique:
                raise ValueError('Select at least one canonical company')
            if execution_adapter not in ('FAKE','DISCOVERY_V2'):
                raise ValueError('Invalid execution adapter')
            if execution_adapter == 'DISCOVERY_V2':
                if not live_confirmed:
                    raise ValueError('Confirm that live web discovery may use Serper')
                if len(unique) > real_company_limit:
                    raise ValueError(f'Live Discovery is limited to {real_company_limit} companies; '
                                     f'{len(unique)} were selected')
            job_id = uuid.uuid4().hex; created = now()
            conn.execute('''INSERT INTO control_jobs(job_id,display_name,input_kind,module,status,created_at,
                queued_at,selected_company_count,execution_adapter) VALUES (?,?,?,?,?,?,?,?,?)''',
                (job_id,name,'UPLOAD','DISCOVERY_CONTACTS','QUEUED',created,created,len(unique),
                 execution_adapter))
            conn.execute("UPDATE upload_rows SET match_status='EXCLUDED',selected=0,match_method=COALESCE(match_method,'UNRESOLVED_EXCLUDED') WHERE upload_id=? AND match_status IN ('AMBIGUOUS','NOT_FOUND')",(upload_id,))
            snapshot_rows = conn.execute('SELECT * FROM upload_rows WHERE upload_id=? ORDER BY row_number',
                                         (upload_id,)).fetchall()
            included = set()
            for position, row in enumerate(snapshot_rows,1):
                selected = int(row['match_status'] == 'MATCHED' and row['selected'] == 1
                               and row['selected_company_id'] not in included)
                if selected:
                    included.add(row['selected_company_id'])
                conn.execute('''INSERT INTO job_items(job_id,item_position,upload_id,upload_row_number,
                    company_id,match_status,match_method,selected) VALUES (?,?,?,?,?,?,?,?)''',
                    (job_id,position,upload_id,row['row_number'],row['selected_company_id'],
                     row['match_status'],row['match_method'],selected))
            conn.execute("UPDATE uploads SET status='CONFIRMED' WHERE upload_id=?",(upload_id,))
            self._event(conn,job_id,'JOB_QUEUED','upload job queued',details={
                'selected_company_count':len(unique),'upload_id':upload_id,'matched_rows':len(rows)})
            conn.commit()
            return self.get_job(job_id)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_import_enrich_job(self, upload_id, display_name, mapping):
        """Snapshot every registration row into the existing durable job queue."""
        name = str(display_name or '').strip()
        if not 1 <= len(name) <= 120:
            raise ValueError('Job name must be between 1 and 120 characters')
        mapping = dict(mapping or {})
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            upload = conn.execute('SELECT * FROM uploads WHERE upload_id=?',
                                  (upload_id,)).fetchone()
            if not upload or upload['status'] not in ('MAPPING', 'REVIEW'):
                raise ValueError('Upload is not available for Import & Enrich')
            headers = json.loads(upload['headers_json'])
            used = [value for value in mapping.values() if value is not None]
            if (any(type(value) is not int or not 0 <= value < len(headers) for value in used)
                    or len(used) != len(set(used))):
                raise ValueError('Invalid or duplicate registration column mapping')
            rows = conn.execute('SELECT * FROM upload_rows WHERE upload_id=? ORDER BY row_number',
                                (upload_id,)).fetchall()
            if not rows:
                raise ValueError('Upload has no registration rows')
            job_id = uuid.uuid4().hex
            created = now()
            conn.execute('''INSERT INTO control_jobs(job_id,display_name,input_kind,module,status,
                created_at,queued_at,selected_company_count,execution_adapter,progress_stage,
                import_total_rows) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (job_id, name, 'UPLOAD', 'IMPORT_ENRICH', 'QUEUED', created, created,
                 len(rows), 'IMPORT_ENRICH', 'PARSING', len(rows)))
            for position, row in enumerate(rows, 1):
                conn.execute('''INSERT INTO job_items(job_id,item_position,upload_id,
                    upload_row_number,company_id,match_status,match_method,selected,
                    processing_status) VALUES (?,?,?,?,NULL,'PENDING',NULL,0,'PENDING')''',
                    (job_id, position, upload_id, row['row_number']))
            conn.execute("UPDATE uploads SET mapping_json=?,status='CONFIRMED' WHERE upload_id=?",
                         (json.dumps(mapping, sort_keys=True, separators=(',', ':')), upload_id))
            self._event(conn, job_id, 'IMPORT_ENRICH_QUEUED',
                        'Import & Enrich matching queued', details={
                            'upload_id': upload_id, 'total_rows': len(rows)})
            conn.commit()
            return self.get_job(job_id)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def pending_import_items(self, job_id):
        conn = self._connect()
        try:
            rows = []
            query = '''SELECT i.*,u.headers_json,u.mapping_json,r.original_values_json
                FROM job_items i JOIN uploads u ON u.upload_id=i.upload_id
                JOIN upload_rows r ON r.upload_id=i.upload_id
                  AND r.row_number=i.upload_row_number
                WHERE i.job_id=? AND i.processing_status='PENDING'
                ORDER BY i.item_position'''
            for stored in conn.execute(query, (job_id,)):
                item = dict(stored)
                item['headers'] = json.loads(item.pop('headers_json'))
                item['mapping'] = json.loads(item.pop('mapping_json'))
                item['original_values'] = json.loads(item.pop('original_values_json'))
                rows.append(item)
            return rows
        finally:
            conn.close()

    def set_import_stage(self, job_id, stage, worker_id):
        allowed = {'PARSING', 'MATCHING', 'EXISTING_ENRICHMENT', 'DISCOVERY',
                   'AI_PREFLIGHT', 'AI_ENRICHMENT', 'EXPORT'}
        if stage not in allowed:
            raise ValueError('Invalid Import & Enrich progress stage')
        conn = self._connect()
        try:
            updated = conn.execute('''UPDATE control_jobs SET progress_stage=?,worker_id=?,
                worker_heartbeat_at=? WHERE job_id=? AND module='IMPORT_ENRICH'
                AND status='RUNNING' ''', (stage, worker_id, now(), job_id)).rowcount
            if updated != 1:
                raise ValueError('Import stage requires a running IMPORT_ENRICH job')
        finally:
            conn.close()

    def complete_import_item(self, job_id, item_position, match, worker_id):
        """Checkpoint one row and recompute the job summary in one transaction."""
        evidence = [dict(item) for item in match.as_dict()['evidence']]
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            job = conn.execute('SELECT * FROM control_jobs WHERE job_id=?',
                               (job_id,)).fetchone()
            if not job or job['module'] != 'IMPORT_ENRICH' or job['status'] != 'RUNNING':
                raise ValueError('Import match requires a running IMPORT_ENRICH job')
            updated = conn.execute('''UPDATE job_items SET processing_status='COMPLETED',
                company_id=?,match_status=?,match_method=?,selected=?,match_evidence_json=?,
                conflicts_json=?,route_hint=?,reusable_enrichment=?,ai_eligibility=?
                WHERE job_id=? AND item_position=? AND processing_status='PENDING' ''',
                (match.company_id, match.outcome, match.match_method,
                 int(match.outcome == 'MATCHED'),
                 json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
                 json.dumps(list(match.conflicts), ensure_ascii=False, separators=(',', ':')),
                 match.route_hint, int(match.has_reusable_enrichment), match.ai_eligibility,
                 job_id, item_position)).rowcount
            if updated == 0:
                # A replayed checkpoint is safe only when the item is already complete.
                existing = conn.execute('''SELECT processing_status FROM job_items
                    WHERE job_id=? AND item_position=?''', (job_id, item_position)).fetchone()
                if not existing:
                    raise ValueError('Unknown Import & Enrich job item')
            summary = self._import_summary_conn(conn, job_id)
            conn.execute('''UPDATE control_jobs SET processed_company_count=?,progress_stage='MATCHING',
                import_matched_count=?,import_ambiguous_count=?,import_unresolved_count=?,
                import_resolved_without_ai_count=?,import_matched_requires_enrichment_count=?,
                worker_id=?,worker_heartbeat_at=? WHERE job_id=?''',
                (summary['processed_rows'], summary['matched'], summary['ambiguous'],
                 summary['unresolved'], summary['resolved_without_ai'],
                 summary['matched_requires_enrichment'], worker_id, now(), job_id))
            conn.commit()
            return summary
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _import_summary_conn(conn, job_id):
        counts = {row['match_status']: row['count'] for row in conn.execute('''
            SELECT match_status,COUNT(*) count FROM job_items
            WHERE job_id=? AND processing_status='COMPLETED' GROUP BY match_status''', (job_id,))}
        routes = {row['route_hint']: row['count'] for row in conn.execute('''
            SELECT route_hint,COUNT(*) count FROM job_items
            WHERE job_id=? AND processing_status='COMPLETED' GROUP BY route_hint''', (job_id,))}
        total = conn.execute('SELECT COUNT(*) FROM job_items WHERE job_id=?',
                             (job_id,)).fetchone()[0]
        processed = sum(counts.values())
        return {'total_rows': total, 'processed_rows': processed,
                'matched': counts.get('MATCHED', 0),
                'ambiguous': counts.get('AMBIGUOUS', 0),
                'unresolved': counts.get('UNRESOLVED', 0),
                'resolved_without_ai': routes.get('RESOLVED_WITHOUT_AI', 0),
                'matched_requires_enrichment': routes.get('MATCHED_REQUIRES_ENRICHMENT', 0)}

    def import_summary(self, job_id):
        conn = self._connect()
        try:
            return self._import_summary_conn(conn, job_id)
        finally:
            conn.close()

    def import_enrichment_company_ids(self, job_id):
        """Distinct future fan-out; registration job items remain undeduplicated."""
        conn = self._connect()
        try:
            return [row[0] for row in conn.execute('''SELECT DISTINCT company_id FROM job_items
                WHERE job_id=? AND processing_status='COMPLETED' AND match_status='MATCHED'
                  AND company_id IS NOT NULL AND route_hint='MATCHED_REQUIRES_ENRICHMENT'
                ORDER BY company_id''', (job_id,))]
        finally:
            conn.close()

    def job_items(self, job_id):
        conn = self._connect()
        try:
            result = []
            for row in conn.execute(
                    'SELECT * FROM job_items WHERE job_id=? ORDER BY item_position',(job_id,)):
                item = dict(row)
                item['match_evidence'] = json.loads(item['match_evidence_json'])
                item['conflicts'] = json.loads(item['conflicts_json'])
                result.append(item)
            return result
        finally:
            conn.close()

    def selected_company_ids(self, job_id):
        return [row['company_id'] for row in self.job_items(job_id) if row['selected'] == 1]

    def set_discovery_run(self, job_id, run_id):
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT discovery_run_id FROM control_jobs WHERE job_id=?',(job_id,)).fetchone()
            if not row:
                raise ValueError('Unknown job')
            if row['discovery_run_id'] and row['discovery_run_id'] != run_id:
                raise ValueError('Discovery run is already fixed for this job')
            conn.execute('UPDATE control_jobs SET discovery_run_id=? WHERE job_id=?',(run_id,job_id))
            if not row['discovery_run_id']:
                self._event(conn,job_id,'DISCOVERY_RUN_CREATED','Discovery v2 run created',
                            details={'run_id':run_id})
            conn.commit()
        except BaseException:
            conn.rollback(); raise
        finally:
            conn.close()

    def update_discovery_progress(self, job_id, counts, worker_id):
        processed = sum(counts.get(key,0) for key in ('COMPLETED','PARTIAL','FAILED','INELIGIBLE'))
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM control_jobs WHERE job_id=?',(job_id,)).fetchone()
            if not row or row['status'] != 'RUNNING':
                raise ValueError('Discovery progress requires a running job')
            if processed > row['selected_company_count']:
                raise ValueError('Discovery progress exceeds selected manifest')
            conn.execute('''UPDATE control_jobs SET processed_company_count=?,completed_company_count=?,
                partial_company_count=?,failed_company_count=?,ineligible_company_count=?,
                emails_found=?,websites_found=?,phones_found=?,worker_id=?,worker_heartbeat_at=?
                WHERE job_id=?''',(processed,counts.get('COMPLETED',0),counts.get('PARTIAL',0),
                counts.get('FAILED',0),counts.get('INELIGIBLE',0),counts.get('emails',0),
                counts.get('websites',0),counts.get('phones',0),worker_id,now(),job_id))
            conn.commit()
        except BaseException:
            conn.rollback(); raise
        finally:
            conn.close()

    def add_artifact(self, job_id, artifact_type, relative_path, size_bytes, sha256):
        artifact_id = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute('''INSERT INTO job_artifacts(artifact_id,job_id,artifact_type,relative_path,
                created_at,size_bytes,sha256) VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_id,artifact_type)
                DO UPDATE SET relative_path=excluded.relative_path,created_at=excluded.created_at,
                size_bytes=excluded.size_bytes,sha256=excluded.sha256''',
                (artifact_id,job_id,artifact_type,relative_path,now(),size_bytes,sha256))
        finally:
            conn.close()

    def artifacts(self, job_id):
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM job_artifacts WHERE job_id=? ORDER BY created_at',(job_id,))]
        finally:
            conn.close()

    def get_artifact(self, artifact_id):
        conn = self._connect()
        try:
            row = conn.execute('SELECT * FROM job_artifacts WHERE artifact_id=?',(artifact_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_job(self, job_id):
        conn = self._connect()
        try:
            row = conn.execute('SELECT * FROM control_jobs WHERE job_id=?', (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_jobs(self, limit=100):
        limit = max(1, min(int(limit), 500))
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM control_jobs ORDER BY job_number DESC LIMIT ?', (limit,))]
        finally:
            conn.close()

    def current_job(self):
        conn = self._connect()
        try:
            row = conn.execute('''SELECT * FROM control_jobs
                ORDER BY CASE WHEN status IN ('STARTING','RUNNING','QUEUED') THEN 0 ELSE 1 END,
                         job_number DESC LIMIT 1''').fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def events(self, job_id, limit=100):
        limit = max(1, min(int(limit), 500))
        conn = self._connect()
        try:
            rows = conn.execute('''SELECT * FROM job_events WHERE job_id=?
                ORDER BY sequence DESC LIMIT ?''', (job_id, limit)).fetchall()
            return [dict(row) for row in reversed(rows)]
        finally:
            conn.close()

    def transition(self, job_id, target, *, worker_id=None, error_code=None,
                   error_message=None, event_code=None, event_message=None):
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT status FROM control_jobs WHERE job_id=?',
                               (job_id,)).fetchone()
            if row is None:
                raise ValueError('Unknown job')
            validate_transition(row['status'], target)
            timestamp = now()
            fields = ['status=?']; values = [target]
            if target == 'RUNNING':
                fields += ['started_at=COALESCE(started_at,?)', 'worker_heartbeat_at=?']
                values += [timestamp, timestamp]
            if target in ('COMPLETED', 'PARTIAL', 'FAILED'):
                fields.append('finished_at=?'); values.append(timestamp)
            if worker_id is not None:
                fields.append('worker_id=?'); values.append(worker_id)
            if target == 'FAILED':
                fields += ['error_code=?', 'error_message=?']
                values += [error_code or 'WORKER_ERROR', (error_message or 'Job failed')[:500]]
            values.append(job_id)
            conn.execute(f'UPDATE control_jobs SET {",".join(fields)} WHERE job_id=?', values)
            self._event(conn, job_id, event_code or 'STATUS_'+target,
                        event_message or target.lower().replace('_', ' '),
                        'ERROR' if target == 'FAILED' else 'INFO')
            conn.commit()
            return self.get_job(job_id)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_oldest(self, worker_id, stale_seconds=30):
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            active = conn.execute("SELECT * FROM control_jobs WHERE status IN ('STARTING','RUNNING') ORDER BY job_number LIMIT 1").fetchone()
            if active is not None:
                try:
                    active_age = (datetime.now(timezone.utc)-datetime.fromisoformat(
                        active['worker_heartbeat_at'])).total_seconds()
                except (TypeError,ValueError):
                    active_age = float('inf')
                worker = conn.execute('SELECT * FROM control_workers WHERE worker_id=?',
                                      (active['worker_id'],)).fetchone()
                online = False
                if worker and worker['status'] == 'ONLINE':
                    try:
                        age = (datetime.now(timezone.utc)-datetime.fromisoformat(worker['heartbeat_at'])).total_seconds()
                        online = age <= stale_seconds
                    except (TypeError,ValueError):
                        pass
                if online or active_age <= stale_seconds:
                    conn.commit(); return None
                timestamp = now()
                conn.execute("UPDATE control_jobs SET status='STARTING',worker_id=?,worker_heartbeat_at=? WHERE job_id=?",
                             (worker_id,timestamp,active['job_id']))
                self._event(conn,active['job_id'],'WORKER_RECOVERED','stale job claimed for recovery',
                            level='WARNING',details={'worker_id':worker_id})
                conn.commit()
                return self.get_job(active['job_id'])
            row = conn.execute("SELECT * FROM control_jobs WHERE status='QUEUED' ORDER BY job_number LIMIT 1").fetchone()
            if row is None:
                conn.commit()
                return None
            timestamp = now()
            updated = conn.execute('''UPDATE control_jobs SET status='STARTING',worker_id=?,
                worker_heartbeat_at=? WHERE job_id=? AND status='QUEUED' ''',
                (worker_id, timestamp, row['job_id'])).rowcount
            if updated != 1:
                conn.rollback()
                return None
            self._event(conn, row['job_id'], 'WORKER_CLAIMED', 'worker claimed job',
                        details={'worker_id': worker_id})
            conn.commit()
            return self.get_job(row['job_id'])
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_progress(self, job_id, processed, emails, websites, phones, worker_id):
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM control_jobs WHERE job_id=?', (job_id,)).fetchone()
            if row is None or row['status'] != 'RUNNING':
                raise ValueError('Progress requires a running job')
            values = (processed, emails, websites, phones)
            if (any(type(value) is not int or value < 0 for value in values)
                    or processed < row['processed_company_count']
                    or processed > row['selected_company_count']):
                raise ValueError('Invalid or regressing job progress')
            timestamp = now()
            conn.execute('''UPDATE control_jobs SET processed_company_count=?,emails_found=?,
                websites_found=?,phones_found=?,worker_id=?,worker_heartbeat_at=?
                WHERE job_id=?''', (*values, worker_id, timestamp, job_id))
            self._event(conn, job_id, 'PROGRESS',
                        f'{processed} / {row["selected_company_count"]} processed',
                        details={'processed': processed, 'emails': emails,
                                 'websites': websites, 'phones': phones})
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def heartbeat(self, worker_id, status='ONLINE'):
        if status not in ('ONLINE', 'STOPPED'):
            raise ValueError('Invalid worker status')
        timestamp = now()
        conn = self._connect()
        try:
            conn.execute('''INSERT INTO control_workers(worker_id,started_at,heartbeat_at,status)
                VALUES (?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET
                heartbeat_at=excluded.heartbeat_at,status=excluded.status''',
                (worker_id, timestamp, timestamp, status))
            if status == 'ONLINE':
                conn.execute("UPDATE control_jobs SET worker_heartbeat_at=? WHERE worker_id=? AND status IN ('STARTING','RUNNING')",
                             (timestamp,worker_id))
        finally:
            conn.close()

    def worker_status(self, stale_seconds=15):
        conn = self._connect()
        try:
            row = conn.execute('SELECT * FROM control_workers ORDER BY heartbeat_at DESC LIMIT 1').fetchone()
            if row is None:
                return {'status': 'OFFLINE', 'worker_id': None, 'heartbeat_at': None}
            data = dict(row)
            try:
                age = (datetime.now(timezone.utc)-datetime.fromisoformat(data['heartbeat_at'])).total_seconds()
            except (TypeError, ValueError):
                age = float('inf')
            status = 'ONLINE' if data['status'] == 'ONLINE' and age <= stale_seconds else 'OFFLINE'
            return {'status': status, 'worker_id': data['worker_id'],
                    'heartbeat_at': data['heartbeat_at']}
        finally:
            conn.close()
