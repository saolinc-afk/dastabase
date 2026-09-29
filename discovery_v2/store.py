"""Separate SQLite storage, immutable input manifests and attempt provenance."""
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from discovery_v2 import ENGINE_VERSION, RULE_VERSION
from discovery_v2.eligibility import company_eligibility
from discovery_v2.models import Config, EXECUTION_MODE, JOB_TYPE

APPLICATION_ID = 0x44563241
SCHEMA_VERSION = 3
ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def snapshot(path, namespace):
    path = Path(path).resolve(strict=True)
    if not namespace.strip():
        raise ValueError("A source namespace is required")
    # A single-file hash is meaningful only for a quiescent, checkpointed source.
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("Source must be a frozen, checkpointed SQLite snapshot")
    before = path.stat()
    with path.open("rb") as handle:
        sha = hashlib.file_digest(handle, "sha256").hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source changed while hashing")
    return {"namespace": namespace, "database_path": str(path), "sha256": sha,
            "size_bytes": after.st_size, "table": "companies_lite"}


def read_manifest(source, ids, namespace):
    ids = list(ids)
    if not ids or len(set(ids)) != len(ids) or any(type(i) is not int or i < 1 for i in ids):
        raise ValueError("Supply unique positive company IDs explicitly")
    descriptor = snapshot(source, namespace)
    conn = readonly(source)
    try:
        companies = []
        for company_id in ids:
            row = conn.execute("""SELECT id,company_name,tax_number,registration_number,address,municipality
                                  FROM companies_lite WHERE id=?""", (company_id,)).fetchone()
            if row is None or not row["company_name"]:
                raise ValueError(f"Missing company identity: {company_id}")
            companies.append(dict(row))
    finally:
        conn.close()
    if descriptor != snapshot(source, namespace):
        raise ValueError("Source changed while reading manifest")
    return descriptor, companies


class Store:
    def __init__(self, path, source=None, create=False, require_new=False):
        original = Path(path).absolute()
        self.path = original.resolve()
        if original.is_symlink() or (self.path.exists() and self.path.stat().st_nlink > 1):
            raise ValueError("Result database must not be a symlink or hard link")
        if self.path.is_relative_to((ROOT / "database").resolve()):
            raise ValueError("Result databases cannot be stored in the production database directory")
        if source and self.path == Path(source).resolve():
            raise ValueError("Result database must differ from input source")
        exists = self.path.exists()
        if require_new and exists:
            raise ValueError("Result database must be a new file")
        if exists:
            check = readonly(self.path)
            try:
                if (check.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or
                        check.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
                    raise ValueError("Refusing to modify a non-Discovery-v2 database")
            finally:
                check.close()
        elif not create:
            raise ValueError("Result database does not exist; create a run first")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation: never initialize an existing file after a race.
            with self.path.open("xb"):
                pass
        self.conn = sqlite3.connect(self.path, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if not exists:
            try:
                self.conn.executescript("BEGIN;\n" + Path(__file__).with_name("schema.sql").read_text() +
                                       f"\nPRAGMA application_id={APPLICATION_ID}; PRAGMA user_version={SCHEMA_VERSION}; COMMIT;")
            except BaseException:
                self.conn.close()
                raise

    def close(self):
        self.conn.close()

    @contextmanager
    def exclusive_runner(self):
        """One local invocation at a time; crash recovery never steals a live attempt."""
        import fcntl
        # A separate inode is necessary: flock conflicts with SQLite locks on macOS.
        lock_path = str(self.path) + '.runner.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'rb') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Another Discovery v2 runner is using this result database") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def insert(self, table, values):
        # All callers supply internal table/column constants, never CLI SQL.
        columns = ",".join(values)
        self.conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in values)})",
                          tuple(values.values()))

    def create_run(self, descriptor, companies, config=None, job_type=JOB_TYPE,
                   execution_mode=EXECUTION_MODE):
        if job_type != JOB_TYPE or execution_mode != EXECUTION_MODE:
            raise ValueError("Phase A supports DISCOVERY_CONTACTS / FRESH_DISCOVERY only")
        config = config or Config()
        if not companies or len({c['id'] for c in companies}) != len(companies):
            raise ValueError("A nonempty unique manifest is required")
        if snapshot(descriptor['database_path'], descriptor['namespace']) != descriptor:
            raise ValueError("Source snapshot no longer matches")
        if self.path == Path(descriptor['database_path']).resolve():
            raise ValueError("Input and result paths must differ")
        run_id = uid()
        with self.conn:
            self.insert("discovery_runs", dict(run_id=run_id, job_type=job_type,
                execution_mode=execution_mode, status="PENDING", engine_version=ENGINE_VERSION,
                rule_version=RULE_VERSION, source_snapshot_json=encode(descriptor),
                config_json=encode(config.as_dict()), config_hash=digest(config.as_dict()),
                manifest_hash=digest(companies), created_at=now()))
            for position, company in enumerate(companies):
                eligibility_status, eligibility_reason = company_eligibility(company)
                self.insert("discovery_run_companies", dict(run_id=run_id, company_id=company['id'],
                    source_namespace=descriptor['namespace'], manifest_position=position,
                    identity_snapshot_json=encode(company), eligibility_status=eligibility_status,
                    eligibility_reason=eligibility_reason,
                    status="INELIGIBLE" if eligibility_status == "INELIGIBLE" else "PENDING"))
        return run_id

    def validate_run(self, run_id):
        row = self.conn.execute("SELECT * FROM discovery_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown run")
        run = dict(row)
        descriptor = json.loads(run['source_snapshot_json'])
        if snapshot(descriptor['database_path'], descriptor['namespace']) != descriptor:
            raise ValueError("Frozen source snapshot changed; resume refused")
        config = json.loads(run['config_json'])
        rows = self.conn.execute("SELECT * FROM discovery_run_companies WHERE run_id=? ORDER BY manifest_position", (run_id,)).fetchall()
        companies = [json.loads(r['identity_snapshot_json']) for r in rows]
        if any(r['company_id'] != company['id'] or r['source_namespace'] != descriptor['namespace'] or r['manifest_position'] != position
               for position, (r, company) in enumerate(zip(rows, companies))):
            raise ValueError("Run manifest identity/namespace integrity check failed")
        if digest(companies) != run['manifest_hash'] or digest(config) != run['config_hash']:
            raise ValueError("Run manifest/config integrity check failed")
        if run['engine_version'] != ENGINE_VERSION or run['rule_version'] != RULE_VERSION:
            raise ValueError("Run implementation version differs; create a new run")
        return run, Config(**config)

    def start_attempt(self, run_id, company_id):
        attempt_id = uid()
        with self.conn:
            number = self.conn.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM discovery_attempts WHERE run_id=? AND company_id=?",
                                       (run_id, company_id)).fetchone()[0]
            self.insert('discovery_attempts', dict(attempt_id=attempt_id, run_id=run_id,
                company_id=company_id, attempt_number=number, status='RUNNING', started_at=now()))
            self.conn.execute("UPDATE discovery_run_companies SET status='RUNNING' WHERE run_id=? AND company_id=?", (run_id, company_id))
        return attempt_id

    def progress(self, attempt_id, diagnostics, counts):
        with self.conn:
            self.conn.execute("UPDATE discovery_attempts SET diagnostics_json=?,request_counts_json=? WHERE attempt_id=?",
                              (encode(diagnostics), encode(counts), attempt_id))

    def record(self, table, context, **values):
        id_column = {'discovery_evidence': 'evidence_id', 'discovery_observations': 'observation_id'}[table]
        record_id = uid()
        with self.conn:
            self.insert(table, {id_column: record_id, **context, **values})
        return record_id

    def _check_refs(self, table, id_column, ids, context):
        for ref in ids:
            found = self.conn.execute(f"SELECT 1 FROM {table} WHERE {id_column}=? AND run_id=? AND attempt_id=? AND company_id=?",
                (ref, context['run_id'], context['attempt_id'], context['company_id'])).fetchone()
            if not found:
                raise ValueError(f"Invalid or cross-attempt provenance reference: {ref}")

    def complete(self, context, contacts, result):
        """Publish this attempt within its run only, atomically with defaults."""
        attempt = self.conn.execute("SELECT status FROM discovery_attempts WHERE attempt_id=? AND run_id=? AND company_id=?",
                                    (context['attempt_id'], context['run_id'], context['company_id'])).fetchone()
        if not attempt or attempt[0] != 'RUNNING':
            raise ValueError("Attempt is not running")
        self._validate_website(context, result)
        with self.conn:
            for contact in contacts:
                refs = json.loads(contact['supporting_observation_ids_json'])
                self._check_refs('discovery_observations', 'observation_id', refs, context)
                if contact['primary_observation_id'] not in refs:
                    raise ValueError("Primary observation must be supporting provenance")
                for ref in refs:
                    observation = self.conn.execute('SELECT observation_type,normalized_value FROM discovery_observations WHERE observation_id=?', (ref,)).fetchone()
                    if tuple(observation) != (contact['contact_type'] + '_CANDIDATE', contact['normalized_value']):
                        raise ValueError("Contact provenance must describe the same contact value/type")
                self.insert('discovery_contacts', {**context, **contact})
            for kind in ('email', 'phone'):
                ref = result.get(f'default_{kind}_contact_id')
                if ref:
                    selected = next((c for c in contacts if c['contact_id'] == ref), None)
                    if not selected or selected['contact_type'] != kind.upper() or selected['attribution_status'] != 'ATTRIBUTED' or 'PERSON' in json.loads(selected['roles_json']):
                        raise ValueError("Defaults require an attributed company contact of the correct type")
            self._check_refs('discovery_evidence', 'evidence_id', json.loads(result['website_evidence_ids_json']), context)
            if result['official_website'] and (result['website_status'] not in ('VERIFIED', 'HIGH', 'MEDIUM')
                                               or not result['website_observation_id']):
                raise ValueError("Usable websites require candidate observation provenance")
            if result['official_website']:
                observation = self.conn.execute('SELECT observation_type,evidence_id FROM discovery_observations WHERE observation_id=?',
                                                (result['website_observation_id'],)).fetchone()
                evidence_refs = json.loads(result['website_evidence_ids_json'])
                if not observation or observation[0] != 'WEBSITE_CANDIDATE' or observation[1] not in evidence_refs:
                    raise ValueError("Website provenance must include the candidate observation's evidence")
            self.insert('discovery_company_results', dict(result_id=uid(), **context, **result, completed_at=now()))
            status = "PARTIAL" if result["contact_outcome"] == "INCOMPLETE" else "COMPLETED"
            self.conn.execute("UPDATE discovery_attempts SET status=?,finished_at=? WHERE attempt_id=?", (status, now(), context["attempt_id"]))
            self.conn.execute("UPDATE discovery_run_companies SET status=?,selected_attempt_id=? WHERE run_id=? AND company_id=?",
                              (status, context['attempt_id'], context['run_id'], context['company_id']))

    def _validate_website(self, context, result):
        from types import SimpleNamespace
        from discovery.domain_generator import normalize_url
        from discovery_v2.ownership import validate_assessment
        usable_status = result.get('website_status') in ('VERIFIED', 'HIGH', 'MEDIUM')
        if not usable_status:
            if result.get('official_website') or result.get('verified_scope'):
                raise ValueError('Unverified result cannot carry an official scope')
            return
        if not result.get('official_website') or result.get('official_website') != result.get('verified_scope'):
            raise ValueError('Verified result requires a consistent official scope')
        args = (context['run_id'], context['company_id'], context['attempt_id'])
        rows = {}
        for stored in self.conn.execute('SELECT * FROM discovery_evidence WHERE run_id=? AND company_id=? AND attempt_id=?', args):
            row = dict(stored)
            row['evidence_payload'] = json.loads(row['evidence_payload_json'])
            rows[row['evidence_id']] = row
        observations = []
        for stored in self.conn.execute('SELECT * FROM discovery_observations WHERE run_id=? AND company_id=? AND attempt_id=? ORDER BY rowid', args):
            row = dict(stored); row['value'] = json.loads(row['value_json']); observations.append(row)
        candidate = next((o for o in observations if o['observation_id'] == result.get('website_observation_id')
                          and o['observation_type'] == 'WEBSITE_CANDIDATE'), None)
        assessment = json.loads(result['website_assessment_json'])
        selected = next((c for c in assessment.get('candidates', [])
                         if c.get('observation_id') == result.get('website_observation_id')), {})
        decision = selected.get('assessment', {})
        company = json.loads(self.conn.execute('SELECT identity_snapshot_json FROM discovery_run_companies WHERE run_id=? AND company_id=?', args[:2]).fetchone()[0])
        writer = SimpleNamespace(evidence_rows=rows, observations=observations)
        fetcher = SimpleNamespace(responses={r['final_url']: True for r in rows.values()
                                  if r['source_kind'] == 'FETCHED_PAGE'}, failures={})
        if (not candidate or normalize_url(selected.get('url')) != normalize_url(candidate['normalized_value']) or
                result['verified_scope'] != decision.get('verified_scope') or
                not validate_assessment(company, candidate['normalized_value'], decision, writer, fetcher)):
            raise ValueError('Verified website requires a valid persisted P1A rule and witnesses')
        evidence_refs = set(json.loads(result['website_evidence_ids_json']))
        if not set(decision['p1a']['supporting_evidence_ids']) <= evidence_refs:
            raise ValueError('Missing decisive website evidence')

    def current_results(self, run_id):
        return [dict(r) for r in self.conn.execute("""SELECT r.* FROM discovery_company_results r
            JOIN discovery_run_companies c ON c.run_id=r.run_id AND c.company_id=r.company_id
            AND c.selected_attempt_id=r.attempt_id WHERE r.run_id=? ORDER BY c.manifest_position""", (run_id,))]
