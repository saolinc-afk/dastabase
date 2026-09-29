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
        conn = self._connect()
        try:
            conn.executescript(Path(__file__).with_name('schema.sql').read_text())
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
        if type(selected_company_count) is not int or not 1 <= selected_company_count <= 1000:
            raise ValueError('Fake company count must be between 1 and 1000')
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
