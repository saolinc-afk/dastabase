"""Isolated Profile result store; canonical and Discovery inputs remain read-only."""
import hashlib, json, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path
from profile_v1 import APPLICATION_ID, ENGINE_VERSION, RULE_VERSION, SCHEMA_VERSION


MIGRATION_2_TO_3 = '''
CREATE TABLE profile_deterministic_results(
 deterministic_result_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
 attempt_id TEXT NOT NULL UNIQUE REFERENCES profile_attempts(attempt_id),
 company_id INTEGER NOT NULL, rule_version TEXT NOT NULL, corpus_hash TEXT NOT NULL,
 corpus_status TEXT NOT NULL CHECK(corpus_status IN
 ('RICH_FIRST_PARTY','THIN_FIRST_PARTY','CANDIDATE_ONLY','SEARCH_ONLY','NO_EVIDENCE')),
 fact_count INTEGER NOT NULL, evidence_count INTEGER NOT NULL, completed_at TEXT NOT NULL
);
CREATE TABLE profile_deterministic_facts(
 fact_id TEXT PRIMARY KEY,
 deterministic_result_id TEXT NOT NULL REFERENCES profile_deterministic_results(deterministic_result_id),
 run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, company_id INTEGER NOT NULL,
 field_name TEXT NOT NULL, value_json TEXT NOT NULL, state TEXT NOT NULL,
 confidence TEXT NOT NULL CHECK(confidence IN ('HIGH','MEDIUM','UNKNOWN')),
 rule_id TEXT NOT NULL, rule_version TEXT NOT NULL,
 UNIQUE(deterministic_result_id,field_name)
);
CREATE TABLE profile_deterministic_fact_evidence(
 fact_id TEXT NOT NULL REFERENCES profile_deterministic_facts(fact_id),
 profile_evidence_id TEXT REFERENCES profile_evidence(evidence_id),
 block_id TEXT REFERENCES profile_content_blocks(block_id),
 origin_database TEXT, origin_run_id TEXT, origin_attempt_id TEXT,
 origin_result_id TEXT, origin_evidence_id TEXT, origin_content_hash TEXT,
 page_url TEXT, source_locator TEXT, exact_quote TEXT, metadata_value TEXT,
 PRIMARY KEY(fact_id,profile_evidence_id,block_id,source_locator,exact_quote,metadata_value)
);
CREATE INDEX profile_deterministic_company ON profile_deterministic_results(run_id,company_id);
CREATE INDEX profile_deterministic_facts_result ON profile_deterministic_facts(deterministic_result_id);
'''

MIGRATION_3_TO_4 = '''
CREATE TABLE profile_lineage_migration_guard(value INTEGER CHECK(value=1));
INSERT INTO profile_lineage_migration_guard SELECT 0 WHERE EXISTS(
 SELECT 1 FROM profile_evidence e LEFT JOIN profile_attempts a
 ON a.attempt_id=e.attempt_id AND a.run_id=e.run_id AND a.company_id=e.company_id
 WHERE a.attempt_id IS NULL);
INSERT INTO profile_lineage_migration_guard SELECT 0 WHERE EXISTS(
 SELECT 1 FROM profile_content_blocks b LEFT JOIN profile_evidence e
 ON e.evidence_id=b.evidence_id AND e.run_id=b.run_id
 AND e.attempt_id=b.attempt_id AND e.company_id=b.company_id
 WHERE e.evidence_id IS NULL);
INSERT INTO profile_lineage_migration_guard SELECT 0 WHERE EXISTS(
 SELECT 1 FROM profile_deterministic_results r LEFT JOIN profile_attempts a
 ON a.attempt_id=r.attempt_id AND a.run_id=r.run_id AND a.company_id=r.company_id
 WHERE a.attempt_id IS NULL);
INSERT INTO profile_lineage_migration_guard SELECT 0 WHERE EXISTS(
 SELECT 1 FROM profile_deterministic_facts f LEFT JOIN profile_deterministic_results r
 ON r.deterministic_result_id=f.deterministic_result_id AND r.run_id=f.run_id
 AND r.attempt_id=f.attempt_id AND r.company_id=f.company_id
 WHERE r.deterministic_result_id IS NULL);
INSERT INTO profile_lineage_migration_guard SELECT 0 WHERE EXISTS(
 SELECT 1 FROM profile_deterministic_fact_evidence fe
 JOIN profile_deterministic_facts f ON f.fact_id=fe.fact_id
 LEFT JOIN profile_evidence e ON e.evidence_id=fe.profile_evidence_id
 AND e.run_id=f.run_id AND e.attempt_id=f.attempt_id AND e.company_id=f.company_id
 WHERE fe.profile_evidence_id IS NOT NULL AND e.evidence_id IS NULL);
INSERT INTO profile_lineage_migration_guard SELECT 0 WHERE EXISTS(
 SELECT 1 FROM profile_deterministic_fact_evidence fe
 JOIN profile_deterministic_facts f ON f.fact_id=fe.fact_id
 LEFT JOIN profile_content_blocks b ON b.block_id=fe.block_id
 AND b.evidence_id=fe.profile_evidence_id AND b.run_id=f.run_id
 AND b.attempt_id=f.attempt_id AND b.company_id=f.company_id
 WHERE fe.block_id IS NOT NULL AND b.block_id IS NULL);
DROP TABLE profile_lineage_migration_guard;

CREATE TRIGGER IF NOT EXISTS profile_run_company_lineage_update
BEFORE UPDATE OF run_id,company_id ON profile_run_companies
WHEN NEW.run_id IS NOT OLD.run_id OR NEW.company_id IS NOT OLD.company_id
BEGIN SELECT RAISE(ABORT,'profile run company lineage is immutable'); END;
CREATE TRIGGER IF NOT EXISTS profile_attempt_lineage_update
BEFORE UPDATE OF attempt_id,run_id,company_id ON profile_attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id OR NEW.run_id IS NOT OLD.run_id
 OR NEW.company_id IS NOT OLD.company_id
BEGIN SELECT RAISE(ABORT,'profile attempt lineage is immutable'); END;

CREATE TRIGGER IF NOT EXISTS profile_evidence_lineage_insert BEFORE INSERT ON profile_evidence
WHEN NOT EXISTS(SELECT 1 FROM profile_attempts a WHERE a.attempt_id=NEW.attempt_id
 AND a.run_id=NEW.run_id AND a.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'profile evidence lineage mismatch'); END;
CREATE TRIGGER IF NOT EXISTS profile_evidence_lineage_update BEFORE UPDATE ON profile_evidence
WHEN NEW.evidence_id IS NOT OLD.evidence_id OR NEW.run_id IS NOT OLD.run_id
 OR NEW.attempt_id IS NOT OLD.attempt_id OR NEW.company_id IS NOT OLD.company_id
 OR NOT EXISTS(SELECT 1 FROM profile_attempts a WHERE a.attempt_id=NEW.attempt_id
 AND a.run_id=NEW.run_id AND a.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'profile evidence lineage is immutable or mismatched'); END;

CREATE TRIGGER IF NOT EXISTS profile_block_lineage_insert BEFORE INSERT ON profile_content_blocks
WHEN NOT EXISTS(SELECT 1 FROM profile_evidence e WHERE e.evidence_id=NEW.evidence_id
 AND e.run_id=NEW.run_id AND e.attempt_id=NEW.attempt_id AND e.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'profile block lineage mismatch'); END;
CREATE TRIGGER IF NOT EXISTS profile_block_lineage_update BEFORE UPDATE ON profile_content_blocks
WHEN NEW.block_id IS NOT OLD.block_id OR NEW.run_id IS NOT OLD.run_id
 OR NEW.attempt_id IS NOT OLD.attempt_id OR NEW.company_id IS NOT OLD.company_id
 OR NEW.evidence_id IS NOT OLD.evidence_id
 OR NOT EXISTS(SELECT 1 FROM profile_evidence e WHERE e.evidence_id=NEW.evidence_id
 AND e.run_id=NEW.run_id AND e.attempt_id=NEW.attempt_id AND e.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'profile block lineage is immutable or mismatched'); END;

CREATE TRIGGER IF NOT EXISTS profile_deterministic_result_lineage_insert BEFORE INSERT ON profile_deterministic_results
WHEN NOT EXISTS(SELECT 1 FROM profile_attempts a WHERE a.attempt_id=NEW.attempt_id
 AND a.run_id=NEW.run_id AND a.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'deterministic result lineage mismatch'); END;
CREATE TRIGGER IF NOT EXISTS profile_deterministic_result_lineage_update BEFORE UPDATE ON profile_deterministic_results
WHEN NEW.deterministic_result_id IS NOT OLD.deterministic_result_id
 OR NEW.run_id IS NOT OLD.run_id OR NEW.attempt_id IS NOT OLD.attempt_id
 OR NEW.company_id IS NOT OLD.company_id
 OR NOT EXISTS(SELECT 1 FROM profile_attempts a WHERE a.attempt_id=NEW.attempt_id
 AND a.run_id=NEW.run_id AND a.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'deterministic result lineage is immutable or mismatched'); END;

CREATE TRIGGER IF NOT EXISTS profile_deterministic_fact_lineage_insert BEFORE INSERT ON profile_deterministic_facts
WHEN NOT EXISTS(SELECT 1 FROM profile_deterministic_results r
 WHERE r.deterministic_result_id=NEW.deterministic_result_id AND r.run_id=NEW.run_id
 AND r.attempt_id=NEW.attempt_id AND r.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'deterministic fact lineage mismatch'); END;
CREATE TRIGGER IF NOT EXISTS profile_deterministic_fact_lineage_update BEFORE UPDATE ON profile_deterministic_facts
WHEN NEW.fact_id IS NOT OLD.fact_id OR NEW.deterministic_result_id IS NOT OLD.deterministic_result_id
 OR NEW.run_id IS NOT OLD.run_id OR NEW.attempt_id IS NOT OLD.attempt_id
 OR NEW.company_id IS NOT OLD.company_id
 OR NOT EXISTS(SELECT 1 FROM profile_deterministic_results r
 WHERE r.deterministic_result_id=NEW.deterministic_result_id AND r.run_id=NEW.run_id
 AND r.attempt_id=NEW.attempt_id AND r.company_id=NEW.company_id)
BEGIN SELECT RAISE(ABORT,'deterministic fact lineage is immutable or mismatched'); END;

CREATE TRIGGER IF NOT EXISTS profile_deterministic_evidence_lineage_insert BEFORE INSERT ON profile_deterministic_fact_evidence
WHEN (NEW.profile_evidence_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f JOIN profile_evidence e
 ON e.evidence_id=NEW.profile_evidence_id AND e.run_id=f.run_id
 AND e.attempt_id=f.attempt_id AND e.company_id=f.company_id
 JOIN profile_run_companies rc ON rc.run_id=f.run_id AND rc.company_id=f.company_id
 WHERE f.fact_id=NEW.fact_id AND e.origin_database IS NEW.origin_database
 AND e.origin_run_id IS NEW.origin_run_id AND e.origin_attempt_id IS NEW.origin_attempt_id
 AND e.origin_evidence_id IS NEW.origin_evidence_id
 AND e.origin_content_hash IS NEW.origin_content_hash
 AND rc.discovery_result_id IS NEW.origin_result_id
 AND COALESCE(e.final_url,e.requested_url) IS NEW.page_url))
 OR (NEW.profile_evidence_id IS NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f
 JOIN profile_run_companies rc ON rc.run_id=f.run_id AND rc.company_id=f.company_id
 JOIN profile_runs pr ON pr.run_id=f.run_id WHERE f.fact_id=NEW.fact_id
 AND rc.discovery_result_id IS NEW.origin_result_id
 AND json_extract(pr.source_descriptor_json,'$.database_path') IS NEW.origin_database
 AND json_extract(pr.source_descriptor_json,'$.run_id') IS NEW.origin_run_id))
 OR (NEW.block_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f JOIN profile_content_blocks b
 ON b.block_id=NEW.block_id AND b.evidence_id=NEW.profile_evidence_id
 AND b.run_id=f.run_id AND b.attempt_id=f.attempt_id AND b.company_id=f.company_id
 WHERE f.fact_id=NEW.fact_id))
BEGIN SELECT RAISE(ABORT,'deterministic evidence lineage mismatch'); END;
CREATE TRIGGER IF NOT EXISTS profile_deterministic_evidence_lineage_update BEFORE UPDATE ON profile_deterministic_fact_evidence
WHEN (NEW.profile_evidence_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f JOIN profile_evidence e
 ON e.evidence_id=NEW.profile_evidence_id AND e.run_id=f.run_id
 AND e.attempt_id=f.attempt_id AND e.company_id=f.company_id
 JOIN profile_run_companies rc ON rc.run_id=f.run_id AND rc.company_id=f.company_id
 WHERE f.fact_id=NEW.fact_id AND e.origin_database IS NEW.origin_database
 AND e.origin_run_id IS NEW.origin_run_id AND e.origin_attempt_id IS NEW.origin_attempt_id
 AND e.origin_evidence_id IS NEW.origin_evidence_id
 AND e.origin_content_hash IS NEW.origin_content_hash
 AND rc.discovery_result_id IS NEW.origin_result_id
 AND COALESCE(e.final_url,e.requested_url) IS NEW.page_url))
 OR (NEW.profile_evidence_id IS NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f
 JOIN profile_run_companies rc ON rc.run_id=f.run_id AND rc.company_id=f.company_id
 JOIN profile_runs pr ON pr.run_id=f.run_id WHERE f.fact_id=NEW.fact_id
 AND rc.discovery_result_id IS NEW.origin_result_id
 AND json_extract(pr.source_descriptor_json,'$.database_path') IS NEW.origin_database
 AND json_extract(pr.source_descriptor_json,'$.run_id') IS NEW.origin_run_id))
 OR (NEW.block_id IS NOT NULL AND NOT EXISTS(
 SELECT 1 FROM profile_deterministic_facts f JOIN profile_content_blocks b
 ON b.block_id=NEW.block_id AND b.evidence_id=NEW.profile_evidence_id
 AND b.run_id=f.run_id AND b.attempt_id=f.attempt_id AND b.company_id=f.company_id
 WHERE f.fact_id=NEW.fact_id))
BEGIN SELECT RAISE(ABORT,'deterministic evidence lineage mismatch'); END;
'''


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
        else:
            application_id=self.conn.execute('PRAGMA application_id').fetchone()[0]
            version=self.conn.execute('PRAGMA user_version').fetchone()[0]
            if application_id != APPLICATION_ID:
                self.conn.close(); raise ValueError('Incompatible Profile result database')
            if version == 2:
                try:
                    self.conn.executescript('BEGIN;\n'+MIGRATION_2_TO_3+MIGRATION_3_TO_4+
                      f'\nPRAGMA user_version={SCHEMA_VERSION};COMMIT;')
                except BaseException:
                    self.conn.close(); raise
            elif version == 3:
                try:
                    self.conn.executescript('BEGIN;\n'+MIGRATION_3_TO_4+
                      f'\nPRAGMA user_version={SCHEMA_VERSION};COMMIT;')
                except BaseException:
                    self.conn.close(); raise
            elif version != SCHEMA_VERSION:
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
