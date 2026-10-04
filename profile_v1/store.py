"""Isolated Profile result store; canonical and Discovery inputs remain read-only."""
import hashlib, json, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path
from profile_v1 import APPLICATION_ID, ENGINE_VERSION, RULE_VERSION, SCHEMA_VERSION


def now(): return datetime.now(timezone.utc).isoformat()
def uid(): return uuid.uuid4().hex
def encode(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def digest(value): return hashlib.sha256(encode(value).encode()).hexdigest()


class Store:
    def __init__(self, path, create=False, require_new=False):
        self.path = Path(path).expanduser().absolute()
        exists = self.path.exists()
        if require_new and exists: raise ValueError('Profile result database must be new')
        if not exists and not create: raise ValueError('Profile result database does not exist')
        if not exists:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.open('xb').close()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        if not exists:
            try:
                self.conn.executescript('BEGIN;\n'+Path(__file__).with_name('schema.sql').read_text()+
                    f'\nPRAGMA application_id={APPLICATION_ID};PRAGMA user_version={SCHEMA_VERSION};COMMIT;')
            except BaseException:
                self.conn.close(); raise
        elif (self.conn.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or
              self.conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
            self.conn.close(); raise ValueError('Incompatible Profile result database')

    def close(self): self.conn.close()

    def insert(self, table, values):
        self.conn.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))

    def create_run(self, descriptor, companies):
        run_id = uid()
        with self.conn:
            self.insert('profile_runs', dict(run_id=run_id, job_type='PROFILE_ACTIVITY',
                status='PENDING', engine_version=ENGINE_VERSION, rule_version=RULE_VERSION,
                source_descriptor_json=encode(descriptor), manifest_hash=digest(companies),
                created_at=now()))
            for position, item in enumerate(companies):
                company = item['identity']
                self.insert('profile_run_companies', dict(run_id=run_id, company_id=company['id'],
                    manifest_position=position, identity_snapshot_json=encode(company),
                    registered_activity_json=(encode(item['registered_activity'])
                                              if item.get('registered_activity') else None),
                    discovery_attempt_id=item['discovery_attempt_id'],
                    discovery_result_id=item['discovery_result_id'],
                    accepted_website=item.get('accepted_website'), status='PENDING'))
        return run_id

    def start_attempt(self, run_id, company_id):
        attempt_id = uid()
        with self.conn:
            number = self.conn.execute('SELECT COALESCE(MAX(attempt_number),0)+1 FROM profile_attempts WHERE run_id=? AND company_id=?',(run_id,company_id)).fetchone()[0]
            self.insert('profile_attempts', dict(attempt_id=attempt_id,run_id=run_id,
                company_id=company_id,attempt_number=number,status='RUNNING',started_at=now()))
            self.conn.execute("UPDATE profile_run_companies SET status='RUNNING' WHERE run_id=? AND company_id=?",(run_id,company_id))
        return attempt_id
