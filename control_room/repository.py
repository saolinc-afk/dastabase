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
        conn = self._connect()
        try:
            conn.executescript(Path(__file__).with_name('schema.sql').read_text())
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
            columns = ','.join(row[1] for row in conn.execute('PRAGMA table_info(control_jobs)'))
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

    def create_upload_job(self, upload_id, display_name):
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
            job_id = uuid.uuid4().hex; created = now()
            conn.execute('''INSERT INTO control_jobs(job_id,display_name,input_kind,module,status,created_at,
                queued_at,selected_company_count) VALUES (?,?,?,?,?,?,?,?)''',
                (job_id,name,'UPLOAD','DISCOVERY_CONTACTS','QUEUED',created,created,len(unique)))
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

    def job_items(self, job_id):
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(
                'SELECT * FROM job_items WHERE job_id=? ORDER BY item_position',(job_id,))]
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

    def claim_oldest(self, worker_id):
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            active = conn.execute("SELECT 1 FROM control_jobs WHERE status IN ('STARTING','RUNNING') LIMIT 1").fetchone()
            if active is not None:
                conn.commit()
                return None
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
